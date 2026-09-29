from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import archive
from .git import GitClient, GitError, GitRemoteError, GitWorktree
from .git_state import (
    archived_removal_checkpoint,
    recover_archived_removal_checkpoint,
    snapshot_git_state,
)
from .github import ExternalServiceError, GhClient
from .locking import repository_lock
from .models import (
    CleanupReservation,
    CommandResult,
    Lease,
    LeaseState,
    PromotionMode,
    Purpose,
    ResolutionState,
)
from .registry import WorktreeRegistry


ArchiveError = archive.ArchiveError


_COMMAND = "wt.archive-discard"
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SYNC_BRANCH = re.compile(r"awf/sync-[0-9a-f]{16}-[0-9a-f]{12}/feature")


@dataclass(frozen=True)
class _Evidence:
    token: str
    payload: dict[str, Any]
    snapshot: dict[str, Any]
    git_state_snapshot: dict[str, Any] | None
    git_state_artifacts: dict[str, Any] | None
    include_uncommitted: bool
    exclude_ignored_paths: tuple[str, ...]
    reason: str
    backup_root: Path
    destination: Path
    head_sha: str

class ArchiveDiscarder:
    """Run the backup-first, local-only archive discard transaction.

    This is deliberately separate from the merged/deployed cleanup flow.  The
    registry reservation is the sole mutable operation before worktree removal;
    the durable archive manifest carries the recovery provenance after that.
    """

    def __init__(
        self,
        *,
        registry: WorktreeRegistry,
        git: GitClient,
        github: GhClient | None,
        cache_dir: Path,
        lock_dir: Path,
        default_base: str | None,
        production_branch: str | None,
    ) -> None:
        self.registry = registry
        self.git = git
        self.github = github
        self.cache_dir = cache_dir.resolve()
        self.lock_dir = lock_dir.resolve()
        self.default_base = default_base
        self.production_branch = production_branch

    def run(
        self,
        lease_id: str,
        *,
        backup_root: Path,
        reason: str,
        preview_token: str | None,
        apply: bool,
        include_uncommitted: bool = False,
        exclude_ignored_paths: tuple[str, ...] = (),
    ) -> CommandResult:
        validation = self._argument_blocker(
            lease_id,
            backup_root,
            reason,
            preview_token,
            apply,
            include_uncommitted,
            exclude_ignored_paths,
        )
        if validation is not None:
            return validation
        try:
            canonical_excluded_paths = self._canonical_exclude_ignored_paths(
                exclude_ignored_paths
            )
        except ValueError:
            return self._blocked(
                "invalid_exclude_ignored_path",
                "exclude_ignored_paths must contain relative, normalized paths.",
            )
        try:
            repository_id = self.git.repository_id()
        except (GitError, OSError):
            return self._blocked(
                "repository_inspection_failed",
                "Unable to inspect the current Git repository.",
            )

        if not apply:
            try:
                lease = self.registry.get_lease_read_only(lease_id)
                reservation = (
                    self.registry.get_cleanup_reservation(lease_id) if lease is not None else None
                )
            except sqlite3.Error:
                return self._blocked("registry_conflict", "Unable to read the worktree registry.")
            if lease is None:
                return self._blocked("unknown_lease", f"Lease {lease_id} does not exist.")
            if reservation is not None:
                return self._preview_reserved(
                    lease,
                    reservation,
                    repository_id=repository_id,
                    backup_root=backup_root,
                    reason=reason,
                    include_uncommitted=include_uncommitted,
                    exclude_ignored_paths=canonical_excluded_paths,
                )
            inspected = self._inspect(
                lease,
                repository_id=repository_id,
                backup_root=backup_root,
                reason=reason,
                original_version=lease.version,
                reservation=None,
                include_uncommitted=include_uncommitted,
                exclude_ignored_paths=canonical_excluded_paths,
            )
            if isinstance(inspected, CommandResult):
                return inspected
            return self._preview(lease, inspected)

        assert preview_token is not None
        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            try:
                lease = self.registry.get_lease(lease_id)
            except sqlite3.Error:
                return self._blocked("registry_conflict", "Unable to read the worktree registry.")
            if lease is None:
                return self._blocked("unknown_lease", f"Lease {lease_id} does not exist.")
            try:
                reservation = self.registry.get_cleanup_reservation(lease.id)
            except sqlite3.Error:
                return self._blocked(
                    "registry_conflict", "Unable to inspect the cleanup reservation.", lease=lease
                )

            if lease.state is LeaseState.REMOVED:
                return self._retry_removed(
                    lease,
                    repository_id=repository_id,
                    backup_root=backup_root,
                    reason=reason,
                    preview_token=preview_token,
                    include_uncommitted=include_uncommitted,
                    exclude_ignored_paths=canonical_excluded_paths,
                )
            if reservation is not None:
                return self._resume_reserved(
                    lease,
                    reservation,
                    repository_id=repository_id,
                    backup_root=backup_root,
                    reason=reason,
                    preview_token=preview_token,
                    include_uncommitted=include_uncommitted,
                    exclude_ignored_paths=canonical_excluded_paths,
                )

            inspected = self._inspect(
                lease,
                repository_id=repository_id,
                backup_root=backup_root,
                reason=reason,
                original_version=lease.version,
                reservation=None,
                include_uncommitted=include_uncommitted,
                exclude_ignored_paths=canonical_excluded_paths,
            )
            if isinstance(inspected, CommandResult):
                return inspected
            if not self._tokens_match(preview_token, inspected.token):
                return self._blocked(
                    "preview_token_mismatch",
                    "The preview token no longer matches the current archive-discard evidence.",
                    lease=lease,
                )
            return self._archive_and_remove(lease, inspected)

    def _argument_blocker(
        self,
        lease_id: str,
        backup_root: Path,
        reason: str,
        preview_token: str | None,
        apply: bool,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
    ) -> CommandResult | None:
        if not isinstance(lease_id, str) or not lease_id:
            return self._blocked("invalid_lease", "lease must be a non-empty string")
        if not isinstance(backup_root, Path) or not backup_root.is_absolute():
            return self._blocked(
                "invalid_backup_root", "backup_root must be an absolute path"
            )
        if not isinstance(reason, str) or not reason.strip():
            return self._blocked("invalid_reason", "reason must be a non-empty string")
        if not isinstance(include_uncommitted, bool):
            return self._blocked(
                "invalid_include_uncommitted",
                "include_uncommitted must be a boolean",
            )
        if not isinstance(exclude_ignored_paths, tuple) or any(
            not isinstance(path, str) for path in exclude_ignored_paths
        ):
            return self._blocked(
                "invalid_exclude_ignored_path",
                "exclude_ignored_paths must be a tuple of paths",
            )
        if apply and (
            not isinstance(preview_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", preview_token) is None
        ):
            return self._blocked(
                "preview_token_required",
                "apply requires the exact preview token returned by a current preview",
            )
        return None

    def _inspect(
        self,
        lease: Lease,
        *,
        repository_id: str,
        backup_root: Path,
        reason: str,
        original_version: int,
        reservation: CleanupReservation | None,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
    ) -> _Evidence | CommandResult:
        repository_root = self._repository_root(lease, repository_id)
        if isinstance(repository_root, CommandResult):
            return repository_root
        eligibility = self._lease_eligibility_blocker(
            lease, repository_id, include_uncommitted=include_uncommitted
        )
        if eligibility is not None:
            return eligibility
        try:
            validated_backup_root = self._validate_backup_root(backup_root)
        except ArchiveError as error:
            return self._archive_blocked(error, lease)
        except (OSError, RuntimeError, ValueError):
            return self._blocked(
                "backup_root_invalid",
                "The backup root could not be safely validated.",
                lease=lease,
            )

        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError):
            return self._blocked(
                "worktree_inspection_failed",
                "Unable to inspect registered Git worktrees.",
                lease=lease,
            )
        worktree_blocker = self._worktree_blocker(lease, repository_root, worktrees)
        if worktree_blocker is not None:
            return worktree_blocker

        try:
            status = self.git.status_porcelain(lease.worktree_path)
            if status and not include_uncommitted:
                return self._blocked(
                    "dirty_worktree",
                    f"Lease {lease.id} has uncommitted, untracked, or conflicted changes.",
                    lease=lease,
                )
            head_sha = self.git.head_sha(lease.worktree_path)
            branch_sha = self.git.resolve_ref(lease.branch)
        except (GitError, OSError):
            return self._blocked(
                "head_unavailable",
                "Unable to inspect the worktree and branch HEAD.",
                lease=lease,
            )
        if _GIT_OBJECT_ID.fullmatch(head_sha) is None or branch_sha != head_sha:
            return self._blocked(
                "branch_head_mismatch",
                f"Lease {lease.id} worktree and local branch no longer have the same HEAD.",
                lease=lease,
            )
        if self._is_imported_scratch_candidate(lease) and head_sha != lease.head_sha:
            return self._blocked(
                "head_mismatch",
                "The imported scratch worktree no longer matches its registered HEAD.",
                lease=lease,
            )
        if reservation is not None and (
            reservation.lease_id != lease.id
            or lease.version != reservation.reserved_version
            or reservation.branch_sha != head_sha
        ):
            return self._blocked(
                "cleanup_reservation_invalid",
                "The cleanup reservation does not match the current worktree identity.",
                lease=lease,
            )

        try:
            remote_repository = self.git.remote_url()
            github = self.github or GhClient(repository_root)
            open_pull_requests = github.find_open_prs(
                head=lease.branch, repository=remote_repository
            )
            remote_sha = self.git.remote_branch_sha(lease.branch)
        except ExternalServiceError:
            return CommandResult.external_error(
                _COMMAND,
                code="github_refresh_failed",
                message="Unable to inspect open pull requests for the branch.",
                lease=lease,
            )
        except GitRemoteError:
            return CommandResult.external_error(
                _COMMAND,
                code="remote_branch_inspection_failed",
                message="Unable to inspect the remote branch.",
                lease=lease,
            )
        except (AttributeError, GitError, OSError, ValueError):
            return self._blocked(
                "preflight_inspection_failed",
                "Unable to inspect archive-discard Git or pull-request state.",
                lease=lease,
            )
        closed_promotion_pr = self._closed_promotion_pr_evidence(
            lease, head_sha, github, repository=remote_repository
        )
        if isinstance(closed_promotion_pr, CommandResult):
            return closed_promotion_pr
        if open_pull_requests:
            return self._blocked(
                "open_pull_request",
                f"Lease {lease.id} branch has an open pull request and cannot be archived.",
                lease=lease,
            )

        try:
            snapshot = archive.snapshot_worktree(
                lease.worktree_path, exclude_ignored_paths=exclude_ignored_paths
            )
        except ArchiveError as error:
            return self._archive_blocked(error, lease)
        except OSError:
            return self._blocked(
                "snapshot_failed",
                "Unable to snapshot the complete worktree safely.",
                lease=lease,
            )
        if not self._valid_snapshot(snapshot):
            return self._blocked(
                "snapshot_invalid",
                "The worktree snapshot was incomplete or malformed.",
                lease=lease,
            )
        try:
            canonical_excluded_paths = self._snapshot_excluded_paths(snapshot)
        except ValueError:
            return self._blocked(
                "snapshot_invalid",
                "The worktree snapshot has an invalid exclusion policy.",
                lease=lease,
            )
        git_state_snapshot: dict[str, Any] | None = None
        if include_uncommitted or canonical_excluded_paths:
            try:
                git_state_snapshot = snapshot_git_state(self.git, lease.worktree_path)
            except (GitError, OSError, RuntimeError, ValueError):
                return self._blocked(
                    "git_state_failed",
                    "Unable to capture the complete Git state for archive discard.",
                    lease=lease,
                )
            if not self._valid_git_state_snapshot(git_state_snapshot):
                return self._blocked(
                    "git_state_failed",
                    "The complete Git state snapshot is malformed.",
                    lease=lease,
                )

        payload = self._token_payload(
            lease,
            version=original_version,
            head_sha=head_sha,
            snapshot=snapshot,
            git_state_snapshot=git_state_snapshot,
            include_uncommitted=include_uncommitted,
            exclude_ignored_paths=canonical_excluded_paths,
            remote_sha=remote_sha,
            closed_promotion_pr=closed_promotion_pr,
            backup_root=validated_backup_root,
            reason=reason,
        )
        if not self._valid_token_payload(payload):
            return self._blocked(
                "preflight_inspection_failed",
                "Unable to validate archive-discard provenance.",
                lease=lease,
            )
        token = self._token(payload)
        return _Evidence(
            token=token,
            payload=payload,
            snapshot=snapshot,
            git_state_snapshot=git_state_snapshot,
            git_state_artifacts=None,
            include_uncommitted=include_uncommitted,
            exclude_ignored_paths=canonical_excluded_paths,
            reason=reason,
            backup_root=validated_backup_root,
            destination=self._archive_destination(validated_backup_root, lease, token),
            head_sha=head_sha,
        )

    def _archive_and_remove(self, lease: Lease, evidence: _Evidence) -> CommandResult:
        metadata = self._metadata(lease, evidence)
        try:
            self._prepare_archive_parent(evidence)
            archive.create_archive(
                destination=evidence.destination,
                worktree_path=lease.worktree_path,
                snapshot=evidence.snapshot,
                metadata=metadata,
                git=self.git,
                git_state_snapshot=evidence.git_state_snapshot,
            )
            archive.verify_archive(
                evidence.destination,
                expected_metadata=metadata,
                expected_snapshot=evidence.snapshot,
                git=self.git,
                git_state_snapshot=evidence.git_state_snapshot,
            )
        except ArchiveError as error:
            return self._archive_blocked(error, lease)
        except (GitError, OSError, RuntimeError, ValueError):
            return self._blocked(
                "backup_verification_failed",
                "The archive backup could not be created and independently verified.",
                lease=lease,
            )

        try:
            revalidated = self._inspect(
                lease,
                repository_id=self.git.repository_id(),
                backup_root=evidence.backup_root,
                reason=evidence.reason,
                original_version=lease.version,
                reservation=None,
                include_uncommitted=evidence.include_uncommitted,
                exclude_ignored_paths=evidence.exclude_ignored_paths,
            )
        except (GitError, OSError, RuntimeError, ValueError):
            return self._blocked(
                "post_backup_drift",
                "The source could not be revalidated before archive cleanup reservation.",
                lease=lease,
            )
        if isinstance(revalidated, CommandResult):
            return revalidated
        if not self._tokens_match(evidence.token, revalidated.token):
            return self._blocked(
                "post_backup_drift",
                "The source changed after the durable backup was created.",
                lease=lease,
            )
        try:
            reservation = self.registry.reserve_cleanup(
                lease.id, expected_version=lease.version, branch_sha=evidence.head_sha
            )
        except (RuntimeError, sqlite3.Error):
            return self._blocked(
                "registry_conflict",
                "Unable to reserve the lease for archive discard.",
                lease=lease,
            )
        return self._remove_reserved(lease, reservation, evidence, metadata=metadata)

    def _preview_reserved(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        *,
        repository_id: str,
        backup_root: Path,
        reason: str,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
    ) -> CommandResult:
        original_version = reservation.reserved_version - 1
        if original_version < 0:
            return self._reserved_blocked(lease, "The cleanup reservation has an invalid version.")
        if self._path_absent(lease):
            return self._reserved_blocked(
                lease,
                "The reserved worktree is absent; supply the original token to resume recovery.",
            )
        inspected = self._inspect(
            lease,
            repository_id=repository_id,
            backup_root=backup_root,
            reason=reason,
            original_version=original_version,
            reservation=reservation,
            include_uncommitted=include_uncommitted,
            exclude_ignored_paths=exclude_ignored_paths,
        )
        if isinstance(inspected, CommandResult):
            return inspected
        try:
            metadata, _snapshot, _git_state_snapshot = self._read_manifest(
                inspected.destination
            )
            if not self._recovery_metadata_matches(
                lease,
                metadata,
                inspected.payload,
                backup_root=inspected.backup_root,
                reason=reason,
                expected_reservation=reservation,
            ):
                return self._reserved_blocked(
                    lease, "The existing archive does not match this cleanup reservation."
                )
            self._verify_existing_archive(lease, inspected, reason, metadata=metadata)
        except ArchiveError as error:
            return self._reserved_archive_blocked(lease, error)
        except (GitError, OSError, RuntimeError, ValueError):
            return self._reserved_blocked(
                lease, "The existing archive cannot be independently verified for recovery."
            )
        return self._preview(lease, inspected)

    def _resume_reserved(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        *,
        repository_id: str,
        backup_root: Path,
        reason: str,
        preview_token: str,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
    ) -> CommandResult:
        original_version = reservation.reserved_version - 1
        if original_version < 0:
            return self._reserved_blocked(lease, "The cleanup reservation has an invalid version.")
        if self._path_absent(lease):
            return self._complete_absent_reservation(
                lease,
                reservation,
                repository_id=repository_id,
                backup_root=backup_root,
                reason=reason,
                preview_token=preview_token,
                include_uncommitted=include_uncommitted,
                exclude_ignored_paths=exclude_ignored_paths,
            )
        recovered = self._verify_recovery_archive(
            lease,
            repository_id=repository_id,
            backup_root=backup_root,
            reason=reason,
            preview_token=preview_token,
            expected_reservation=reservation,
            include_uncommitted=include_uncommitted,
            exclude_ignored_paths=exclude_ignored_paths,
        )
        if isinstance(recovered, CommandResult):
            return recovered
        try:
            self._recover_reserved_checkpoint(lease, recovered)
        except (ArchiveError, GitError, OSError, RuntimeError, ValueError):
            return self._reserved_blocked(
                lease,
                "The interrupted Git state checkpoint could not be restored safely.",
            )
        inspected = self._inspect(
            lease,
            repository_id=repository_id,
            backup_root=backup_root,
            reason=reason,
            original_version=original_version,
            reservation=reservation,
            include_uncommitted=include_uncommitted,
            exclude_ignored_paths=exclude_ignored_paths,
        )
        if isinstance(inspected, CommandResult):
            return inspected
        if not self._tokens_match(preview_token, inspected.token):
            return self._reserved_blocked(
                lease,
                "The supplied token does not match this reserved archive-discard operation.",
            )
        try:
            metadata, _snapshot, _git_state_snapshot = self._read_manifest(
                inspected.destination
            )
            if not self._recovery_metadata_matches(
                lease,
                metadata,
                inspected.payload,
                backup_root=inspected.backup_root,
                reason=reason,
                expected_reservation=reservation,
            ):
                return self._reserved_blocked(
                    lease, "The existing archive does not match this cleanup reservation."
                )
            self._verify_existing_archive(lease, inspected, reason, metadata=metadata)
        except ArchiveError as error:
            return self._reserved_archive_blocked(lease, error)
        except (GitError, OSError, RuntimeError, ValueError):
            return self._reserved_blocked(
                lease, "The existing archive cannot be independently verified for recovery."
            )
        return self._remove_reserved(lease, reservation, inspected, metadata=metadata)

    def _remove_reserved(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        evidence: _Evidence,
        *,
        metadata: dict[str, Any],
    ) -> CommandResult:
        branch_hold = (
            self.git.hold_branch_if_at(lease.branch, reservation.branch_sha)
            if evidence.git_state_snapshot is not None
            else self.git.hold_worktree_branch_if_at(
                lease.worktree_path, lease.branch, reservation.branch_sha
            )
        )
        try:
            with branch_hold:
                try:
                    current = self.registry.get_lease(lease.id)
                    current_reservation = self.registry.get_cleanup_reservation(lease.id)
                except sqlite3.Error:
                    return self._reserved_blocked(
                        lease, "Unable to revalidate the cleanup reservation."
                    )
                if current is None or current_reservation != reservation:
                    return self._release_reservation(
                        lease,
                        reservation,
                        "The lease changed while archive discard was reserved.",
                    )
                try:
                    archive_manifest = self._verify_existing_archive(
                        current, evidence, evidence.reason, metadata=metadata
                    )
                    checkpoint_git_state = self._checkpoint_git_state(
                        archive_manifest, evidence.git_state_snapshot
                    )
                except ArchiveError as error:
                    return self._release_reservation(
                        lease,
                        reservation,
                        f"The durable archive could not be reverified ({error.code}).",
                    )
                except (GitError, OSError, RuntimeError, ValueError):
                    return self._release_reservation(
                        lease,
                        reservation,
                        "The durable archive could not be reverified.",
                    )
                try:
                    current = self.registry.get_lease(lease.id)
                    current_reservation = self.registry.get_cleanup_reservation(lease.id)
                except sqlite3.Error:
                    return self._reserved_blocked(
                        lease, "Unable to revalidate the cleanup reservation."
                    )
                if current is None or current_reservation != reservation:
                    return self._release_reservation(
                        lease,
                        reservation,
                        "The lease changed while the durable archive was verified.",
                    )
                inspected = self._inspect(
                    current,
                    repository_id=self.git.repository_id(),
                    backup_root=evidence.backup_root,
                    reason=evidence.reason,
                    original_version=reservation.reserved_version - 1,
                    reservation=reservation,
                    include_uncommitted=evidence.include_uncommitted,
                    exclude_ignored_paths=evidence.exclude_ignored_paths,
                )
                if isinstance(inspected, CommandResult) or not self._tokens_match(
                    evidence.token, inspected.token
                ):
                    return self._release_reservation(
                        lease,
                        reservation,
                        "The worktree changed after the durable backup was created.",
                    )
                try:
                    if checkpoint_git_state is None:
                        self.git.remove_worktree(current.worktree_path)
                    else:
                        checkpoint_snapshot = evidence.git_state_snapshot
                        if checkpoint_snapshot is None:
                            return self._release_reservation(
                                current,
                                reservation,
                                "The archive Git state checkpoint has no matching snapshot.",
                            )
                        with archived_removal_checkpoint(
                            self.git,
                            current.worktree_path,
                            snapshot=checkpoint_snapshot,
                            git_state=checkpoint_git_state,
                            private_root=evidence.backup_root,
                            exclude_ignored_paths=evidence.exclude_ignored_paths,
                        ) as environment:
                            self.git.remove_worktree(
                                current.worktree_path, env=environment
                            )
                except (ArchiveError, GitError, OSError, RuntimeError, ValueError):
                    return self._release_after_remove_failure(
                        current, reservation, evidence
                    )
        except GitError:
            return self._release_reservation(
                lease,
                reservation,
                "The branch or worktree HEAD changed before archive discard could remove it.",
            )
        return self._complete_removed(lease, reservation, evidence)

    def _release_after_remove_failure(
        self, lease: Lease, reservation: CleanupReservation, evidence: _Evidence
    ) -> CommandResult:
        try:
            inspected = self._inspect(
                lease,
                repository_id=self.git.repository_id(),
                backup_root=evidence.backup_root,
                reason=evidence.reason,
                original_version=reservation.reserved_version - 1,
                reservation=reservation,
                include_uncommitted=evidence.include_uncommitted,
                exclude_ignored_paths=evidence.exclude_ignored_paths,
            )
        except (GitError, OSError, RuntimeError, ValueError, sqlite3.Error):
            return self._reserved_blocked(
                lease,
                "Worktree removal failed and the source cannot be revalidated; the cleanup reservation was retained.",
            )
        if isinstance(inspected, CommandResult) or not self._tokens_match(
            evidence.token, inspected.token
        ):
            return self._reserved_blocked(
                lease,
                "Worktree removal failed and the source cannot be revalidated; the cleanup reservation was retained.",
            )
        return self._release_reservation(
            lease,
            reservation,
            "Worktree removal failed; the unchanged source and verified backup were retained.",
            code="remove_worktree_failed",
        )

    def _complete_removed(
        self, lease: Lease, reservation: CleanupReservation, evidence: _Evidence
    ) -> CommandResult:
        try:
            removed = self.registry.complete_cleanup(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, ValueError, sqlite3.Error):
            return self._reserved_blocked(
                lease,
                "The worktree was removed, but registry completion failed; retry with the same token to recover safely.",
            )
        return self._finish_branch_cleanup(removed, evidence)

    def _complete_absent_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        *,
        repository_id: str,
        backup_root: Path,
        reason: str,
        preview_token: str,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
    ) -> CommandResult:
        verified = self._verify_recovery_archive(
            lease,
            repository_id=repository_id,
            backup_root=backup_root,
            reason=reason,
            preview_token=preview_token,
            expected_reservation=reservation,
            include_uncommitted=include_uncommitted,
            exclude_ignored_paths=exclude_ignored_paths,
        )
        if isinstance(verified, CommandResult):
            return verified
        evidence = verified
        try:
            self._recover_reserved_checkpoint(lease, evidence)
        except (ArchiveError, GitError, OSError, RuntimeError, ValueError):
            return self._reserved_blocked(
                lease,
                "The completed Git state checkpoint could not be cleaned safely.",
            )
        try:
            if self.git.resolve_ref(lease.branch) != reservation.branch_sha:
                return self._reserved_blocked(
                    lease, "The local branch changed after the worktree was removed."
                )
        except GitError:
            return self._reserved_blocked(
                lease, "The local branch is unavailable for cleanup recovery.")
        return self._complete_removed(lease, reservation, evidence)

    def _retry_removed(
        self,
        lease: Lease,
        *,
        repository_id: str,
        backup_root: Path,
        reason: str,
        preview_token: str,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
    ) -> CommandResult:
        verified = self._verify_recovery_archive(
            lease,
            repository_id=repository_id,
            backup_root=backup_root,
            reason=reason,
            preview_token=preview_token,
            expected_reservation=None,
            include_uncommitted=include_uncommitted,
            exclude_ignored_paths=exclude_ignored_paths,
        )
        if isinstance(verified, CommandResult):
            return verified
        return self._finish_branch_cleanup(lease, verified)

    def _verify_recovery_archive(
        self,
        lease: Lease,
        *,
        repository_id: str,
        backup_root: Path,
        reason: str,
        preview_token: str,
        expected_reservation: CleanupReservation | None,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
    ) -> _Evidence | CommandResult:
        repository_root = self._repository_root(lease, repository_id)
        if isinstance(repository_root, CommandResult):
            return repository_root
        try:
            validated_backup_root = self._validate_backup_root(backup_root)
            destination = self._archive_destination(validated_backup_root, lease, preview_token)
            metadata, snapshot, git_state_snapshot = self._read_manifest(destination)
        except ArchiveError as error:
            return self._archive_blocked(error, lease)
        except (OSError, RuntimeError, ValueError):
            return self._blocked(
                "backup_verification_failed",
                "The recovery archive manifest could not be read safely.",
                lease=lease,
            )
        payload = metadata.get("archive_discard_evidence")
        if not isinstance(payload, dict) or not self._valid_token_payload(payload):
            return self._blocked(
                "archive_provenance_mismatch",
                "The durable archive has invalid archive-discard provenance.",
                lease=lease,
            )
        if not self._tokens_match(preview_token, self._token(payload)):
            return self._blocked(
                "preview_token_mismatch",
                "The supplied token does not match the durable archive provenance.",
                lease=lease,
            )
        recorded_include_uncommitted, recorded_excluded_paths = self._payload_policy(payload)
        if (
            recorded_include_uncommitted != include_uncommitted
            or recorded_excluded_paths != exclude_ignored_paths
        ):
            return self._blocked(
                "archive_provenance_mismatch",
                "The durable archive does not match the requested discard policy.",
                lease=lease,
            )
        if not self._recovery_metadata_matches(
            lease,
            metadata,
            payload,
            backup_root=validated_backup_root,
            reason=reason,
            expected_reservation=expected_reservation,
        ):
            return self._blocked(
                "archive_provenance_mismatch",
                "The durable archive does not belong to this lease, path, branch, or reason.",
                lease=lease,
            )
        head_sha = payload.get("head_sha")
        if not isinstance(head_sha, str) or _GIT_OBJECT_ID.fullmatch(head_sha) is None:
            return self._blocked(
                "archive_provenance_mismatch",
                "The durable archive does not have a valid recorded HEAD.",
                lease=lease,
            )
        recorded_snapshot = payload.get("snapshot")
        if (
            not isinstance(recorded_snapshot, dict)
            or not self._valid_snapshot(recorded_snapshot)
            or recorded_snapshot != snapshot
        ):
            return self._blocked(
                "archive_provenance_mismatch",
                "The durable archive does not match the token-bound full snapshot.",
                lease=lease,
            )
        if self._payload_uses_git_state(payload):
            if (
                not isinstance(git_state_snapshot, dict)
                or not self._valid_git_state_snapshot(git_state_snapshot)
            ):
                return self._blocked(
                    "archive_provenance_mismatch",
                    "The durable archive does not match the token-bound Git state.",
                    lease=lease,
                )
            git_state_fingerprint = git_state_snapshot.get("fingerprint")
            if (
                not isinstance(git_state_fingerprint, str)
                or payload["git_state_fingerprint"] != git_state_fingerprint
            ):
                return self._blocked(
                    "archive_provenance_mismatch",
                    "The durable archive does not match the token-bound Git state.",
                    lease=lease,
                )
        elif git_state_snapshot is not None:
            return self._blocked(
                "archive_provenance_mismatch",
                "The durable archive does not match the token-bound Git state.",
                lease=lease,
            )
        evidence = _Evidence(
            token=preview_token,
            payload=payload,
            snapshot=snapshot,
            git_state_snapshot=git_state_snapshot,
            git_state_artifacts=None,
            include_uncommitted=recorded_include_uncommitted,
            exclude_ignored_paths=recorded_excluded_paths,
            reason=reason,
            backup_root=validated_backup_root,
            destination=destination,
            head_sha=head_sha,
        )
        try:
            archive_manifest = self._verify_existing_archive(
                lease, evidence, reason, metadata=metadata
            )
            checkpoint_git_state = self._checkpoint_git_state(
                archive_manifest, git_state_snapshot
            )
        except ArchiveError as error:
            return self._archive_blocked(error, lease)
        except (GitError, OSError, RuntimeError, ValueError):
            return self._blocked(
                "backup_verification_failed",
                "The recovery archive could not be independently verified.",
                lease=lease,
            )
        evidence = _Evidence(
            token=evidence.token,
            payload=evidence.payload,
            snapshot=evidence.snapshot,
            git_state_snapshot=evidence.git_state_snapshot,
            git_state_artifacts=checkpoint_git_state,
            include_uncommitted=evidence.include_uncommitted,
            exclude_ignored_paths=evidence.exclude_ignored_paths,
            reason=evidence.reason,
            backup_root=evidence.backup_root,
            destination=evidence.destination,
            head_sha=evidence.head_sha,
        )
        try:
            remote_repository = self.git.remote_url()
            github = self.github or GhClient(repository_root)
            open_pull_requests = github.find_open_prs(
                head=lease.branch, repository=remote_repository
            )
            remote_sha = self.git.remote_branch_sha(lease.branch)
        except ExternalServiceError:
            return CommandResult.external_error(
                _COMMAND,
                code="github_refresh_failed",
                message="Unable to inspect open pull requests for recovery.",
                lease=lease,
            )
        except GitRemoteError:
            return CommandResult.external_error(
                _COMMAND,
                code="remote_branch_inspection_failed",
                message="Unable to inspect the remote branch for recovery.",
                lease=lease,
            )
        except (AttributeError, GitError, OSError, ValueError):
            return self._blocked(
                "preflight_inspection_failed",
                "Unable to inspect archive-discard state for recovery.",
                lease=lease,
            )
        closed_promotion_pr = self._closed_promotion_pr_evidence(
            lease, head_sha, github, repository=remote_repository
        )
        if isinstance(closed_promotion_pr, CommandResult):
            return closed_promotion_pr
        if open_pull_requests:
            return self._blocked(
                "open_pull_request",
                f"Lease {lease.id} branch has an open pull request and cannot be archived.",
                lease=lease,
            )
        if payload["closed_promotion_pr"] != closed_promotion_pr:
            return self._blocked(
                "archive_provenance_mismatch",
                "The durable archive does not match the current closed promotion pull request.",
                lease=lease,
            )
        if remote_sha != payload.get("remote_sha"):
            return self._blocked(
                "remote_branch_changed",
                "The remote branch changed after the archive-discard preview.",
                lease=lease,
            )
        return evidence

    def _finish_branch_cleanup(self, lease: Lease, evidence: _Evidence) -> CommandResult:
        actions: list[dict[str, object]] = [
            self._archive_action("create_archive", lease, evidence),
            self._action("remove_worktree", lease, evidence.head_sha),
            self._action("complete_cleanup", lease, evidence.head_sha),
        ]
        warnings: list[dict[str, str]] = []
        if self._deletes_local_branch(lease):
            try:
                current_sha = self.git.local_branch_sha(lease.branch)
                if current_sha is None:
                    actions.append(
                        {
                            **self._action("local_branch_already_absent", lease, evidence.head_sha),
                            "idempotent": True,
                        }
                    )
                else:
                    self.git.delete_inactive_branch_if_at(lease.branch, evidence.head_sha)
                    actions.append(self._action("delete_local_branch", lease, evidence.head_sha))
            except (GitError, OSError):
                warnings.append(
                    {
                        "code": "local_branch_cleanup_failed",
                        "message": "The worktree was archived and removed, but local branch deletion must be retried with the same token after resolving the checkout or branch mismatch.",
                    }
                )
        else:
            actions.append(self._action("preserve_local_branch", lease, evidence.head_sha))
        return CommandResult.ok(
            _COMMAND,
            decision="removed",
            lease=lease,
            actions=tuple(actions),
            warnings=tuple(warnings),
        )

    def _preview(self, lease: Lease, evidence: _Evidence) -> CommandResult:
        actions: list[dict[str, object]] = [
            self._archive_action("create_archive", lease, evidence),
            self._action("reserve_cleanup", lease, evidence.head_sha),
            self._action("remove_worktree", lease, evidence.head_sha),
            self._action("complete_cleanup", lease, evidence.head_sha),
        ]
        actions.append(
            self._action(
                "delete_local_branch" if self._deletes_local_branch(lease) else "preserve_local_branch",
                lease,
                evidence.head_sha,
            )
        )
        return CommandResult.ok(
            _COMMAND, decision="preview", lease=lease, actions=tuple(actions)
        )

    def _repository_root(
        self, lease: Lease, repository_id: str
    ) -> Path | CommandResult:
        try:
            repository_root = self.git.repository_root().resolve()
            repository_name = self.git.repository_name()
        except (GitError, OSError, RuntimeError):
            return self._blocked(
                "repository_inspection_failed",
                "Unable to inspect the current Git repository.",
                lease=lease,
            )
        try:
            lease_root = lease.repository_root.resolve()
        except (OSError, RuntimeError):
            return self._blocked(
                "repository_mismatch",
                "The lease repository root cannot be safely resolved.",
                lease=lease,
            )
        if (
            lease.repository_id != repository_id
            or lease.repository_name != repository_name
            or lease_root != repository_root
        ):
            return self._blocked(
                "repository_mismatch",
                "The lease does not belong to the current Git repository.",
                lease=lease,
            )
        return repository_root

    def _lease_eligibility_blocker(
        self, lease: Lease, repository_id: str, *, include_uncommitted: bool
    ) -> CommandResult | None:
        if lease.repository_id != repository_id:
            return self._blocked(
                "repository_mismatch", "The lease belongs to a different repository.", lease=lease
            )
        if not self._canonical_uuid(lease.id):
            return self._blocked(
                "unsafe_lease_id",
                "Archive discard requires a canonical lease identifier.",
                lease=lease,
            )
        if lease.retain:
            return self._blocked(
                "retained_lease", "Retained leases cannot be archive-discarded.", lease=lease
            )
        if lease.initiative.startswith("release-"):
            try:
                release = self.registry.find_release_read_only(
                    lease.repository_id, lease.initiative.removeprefix("release-")
                )
            except sqlite3.Error:
                return self._blocked(
                    "registry_conflict",
                    "Unable to inspect release ownership for archive discard.",
                    lease=lease,
                )
            if release is not None and release.lease_id == lease.id:
                return self._blocked(
                    "release_lease", "Release worktrees cannot be archive-discarded.", lease=lease
                )
        if self._is_sync_lease(lease):
            return self._blocked(
                "sync_lease", "Synchronization worktrees cannot be archive-discarded.", lease=lease
            )
        legacy_retry = self._is_legacy_promotion_retry_path(lease)
        completed_closed_promotion = (
            self._requires_closed_promotion_pr_evidence(lease)
            and lease.state is LeaseState.CLOSED_UNMERGED
        )
        blocked_manual = self._is_blocked_manual_candidate(lease, legacy_retry)
        manual_state = (
            lease.resolution_state is not ResolutionState.NONE
            or lease.conflicted_paths
            or lease.protected_index_entries
            or lease.conflict_source_ordinal is not None
        )
        if manual_state and not completed_closed_promotion and not (
            include_uncommitted and blocked_manual
        ):
            return self._blocked(
                "manual_resolution_present",
                "Leases with conflict or manual-resolution state require --include-uncommitted.",
                lease=lease,
            )
        if self._is_awf_candidate(lease):
            if lease.purpose is Purpose.FEATURE and lease.state is LeaseState.ACTIVE:
                return None
            if not lease.branch.startswith("awf/"):
                return self._blocked(
                    "unsafe_branch", "AWF archive discard requires an AWF local branch.", lease=lease
                )
            if lease.purpose is Purpose.PROMOTE and lease.state in {
                LeaseState.BLOCKED,
                LeaseState.CLOSED_UNMERGED,
            }:
                if manual_state and not completed_closed_promotion and not include_uncommitted:
                    return self._blocked(
                        "include_uncommitted_required",
                        "Blocked manual promotions require --include-uncommitted.",
                        lease=lease,
                    )
                return None
            return self._blocked(
                "unsupported_state",
                "Only active feature or abandoned blocked/closed-unmerged promotion leases are eligible.",
                lease=lease,
            )
        if self._is_imported_candidate(lease):
            return None
        if self._is_dirty_imported_candidate(lease):
            if include_uncommitted:
                return None
            return self._blocked(
                "include_uncommitted_required",
                "Dirty imported scratch worktrees require --include-uncommitted.",
                lease=lease,
            )
        if legacy_retry:
            if include_uncommitted and lease.state is LeaseState.BLOCKED:
                return None
            return self._blocked(
                "include_uncommitted_required",
                "Legacy promotion retries require --include-uncommitted.",
                lease=lease,
            )
        return self._blocked(
            "foreign_owner",
            "Only AWF-owned candidates or imported scratch worktrees are eligible.",
            lease=lease,
        )

    def _closed_promotion_pr_evidence(
        self, lease: Lease, head_sha: str, github: GhClient, *, repository: str
    ) -> dict[str, Any] | CommandResult | None:
        if not self._requires_closed_promotion_pr_evidence(lease):
            return None
        if (
            not isinstance(lease.target_pr, int)
            or isinstance(lease.target_pr, bool)
            or lease.target_pr <= 0
        ):
            return self._blocked(
                "closed_promotion_pr_missing",
                "A completed closed promotion requires its target pull request.",
                lease=lease,
            )
        try:
            pull_request = github.view_pr(lease.target_pr, repository=repository)
        except ExternalServiceError:
            return CommandResult.external_error(
                _COMMAND,
                code="github_refresh_failed",
                message="Unable to inspect the completed closed promotion pull request.",
                lease=lease,
            )
        except (AttributeError, KeyError, OSError, ValueError):
            return self._blocked(
                "preflight_inspection_failed",
                "Unable to inspect the completed closed promotion pull request.",
                lease=lease,
            )
        if (
            pull_request.number != lease.target_pr
            or pull_request.state != "CLOSED"
            or pull_request.merge_commit_sha is not None
        ):
            return self._blocked(
                "closed_promotion_pr_mismatch",
                "The promotion target pull request is not closed without merging.",
                lease=lease,
            )
        if pull_request.head_ref != lease.branch or pull_request.head_sha != head_sha:
            return self._blocked(
                "closed_promotion_pr_mismatch",
                "The closed promotion pull request does not match the current branch and HEAD.",
                lease=lease,
            )
        return {
            "number": pull_request.number,
            "state": pull_request.state,
            "head_ref": pull_request.head_ref,
            "head_sha": pull_request.head_sha,
            "merge_commit_sha": pull_request.merge_commit_sha,
            "repository_sha256": hashlib.sha256(
                repository.encode("utf-8")
            ).hexdigest(),
        }

    def _worktree_blocker(
        self, lease: Lease, repository_root: Path, worktrees: tuple[GitWorktree, ...]
    ) -> CommandResult | None:
        path_blocker = self._worktree_path_blocker(lease, repository_root)
        if path_blocker is not None:
            return path_blocker
        try:
            expected_path = lease.worktree_path.resolve()
        except (OSError, RuntimeError):
            return self._blocked(
                "unsafe_worktree_path", "The worktree path cannot be resolved safely.", lease=lease
            )
        registered = tuple(
            item for item in worktrees if self._same_path(item.path, expected_path)
        )
        if len(registered) != 1:
            return self._blocked(
                "unregistered_worktree", "The lease is not registered at its exact worktree path.", lease=lease
            )
        worktree = registered[0]
        if worktree.bare or worktree.detached or worktree.prunable:
            return self._blocked(
                "unregistered_worktree", "The registered worktree is not an active branch checkout.", lease=lease
            )
        if worktree.locked is not None:
            return self._blocked(
                "locked_worktree", "The worktree is locked and cannot be archive-discarded.", lease=lease
            )
        if worktree.branch != lease.branch:
            return self._blocked(
                "branch_mismatch", "The registered worktree does not own the lease branch.", lease=lease
            )
        if self._is_imported_scratch_candidate(lease) and worktree.head_sha != lease.head_sha:
            return self._blocked(
                "head_mismatch",
                "The imported scratch worktree does not match its registered HEAD.",
                lease=lease,
            )
        if self._same_path(expected_path, repository_root):
            return self._blocked(
                "root_worktree", "The repository root checkout cannot be archive-discarded.", lease=lease
            )
        if any(
            not self._same_path(item.path, expected_path) and item.branch == lease.branch
            for item in worktrees
        ):
            return self._blocked(
                "branch_in_use", "The lease branch is checked out by another worktree.", lease=lease
            )
        try:
            protected = self._protected_branches(worktrees, repository_root, lease)
        except (GitError, OSError):
            return self._blocked(
                "protected_branch_unavailable",
                "The remote default branch cannot be inspected safely.",
                lease=lease,
            )
        if lease.branch in protected:
            return self._blocked(
                "protected_branch", "The lease branch is protected from archive discard.", lease=lease
            )
        return None

    def _worktree_path_blocker(
        self, lease: Lease, repository_root: Path
    ) -> CommandResult | None:
        if not self._path_has_no_symlink_ancestors(lease.worktree_path):
            return self._blocked(
                "unsafe_worktree_path",
                "The worktree path has a symlinked or unsafe ancestor.",
                lease=lease,
            )
        try:
            worktree_path = lease.worktree_path.resolve(strict=True)
            root = repository_root.resolve()
        except (OSError, RuntimeError):
            return self._blocked(
                "unsafe_worktree_path",
                "The worktree path cannot be safely inspected.",
                lease=lease,
            )
        if worktree_path == root:
            return self._blocked(
                "root_worktree",
                "The repository root checkout cannot be archive-discarded.",
                lease=lease,
            )
        managed_cache_path = (
            worktree_path == self.cache_dir / lease.repository_name / lease.id
            and self._canonical_uuid(lease.id)
        )
        if (self._is_awf_candidate(lease) or self._is_legacy_promotion_retry_path(lease)) and not (
            managed_cache_path or self._is_legacy_promotion_retry_path(lease)
        ):
            return self._blocked(
                "unsafe_worktree_path",
                "The AWF lease does not use its exact managed cache path.",
                lease=lease,
            )
        try:
            details = worktree_path.lstat()
            parent = worktree_path.parent.lstat()
        except OSError:
            return self._blocked(
                "worktree_inspection_failed",
                "The worktree path could not be inspected.",
                lease=lease,
            )
        if not stat.S_ISDIR(details.st_mode) or not stat.S_ISDIR(parent.st_mode):
            return self._blocked(
                "unregistered_worktree",
                "The worktree root is not a directory.",
                lease=lease,
            )
        return None

    def _validate_backup_root(self, backup_root: Path) -> Path:
        try:
            leases = self.registry.list_leases_read_only(include_removed=False)
        except sqlite3.Error as error:
            raise ArchiveError(
                "registry_conflict", "unable to identify forbidden backup roots"
            ) from error
        forbidden = [self.git.repository_root(), self.cache_dir]
        forbidden.extend(lease.worktree_path for lease in leases)
        return archive.validate_backup_root(backup_root, forbidden_roots=tuple(forbidden))

    def _metadata(self, lease: Lease, evidence: _Evidence) -> dict[str, Any]:
        return {
            "head_sha": evidence.head_sha,
            "lease": lease.to_dict(),
            "repository_id": lease.repository_id,
            "reason": evidence.reason,
            "preview_token": evidence.token,
            "archive_discard_evidence": evidence.payload,
        }

    def _verify_existing_archive(
        self,
        lease: Lease,
        evidence: _Evidence,
        reason: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._valid_token_payload(evidence.payload):
            raise ArchiveError(
                "archive_provenance_mismatch", "archive evidence has an invalid shape"
            )
        expected_metadata = metadata or self._metadata(lease, evidence)
        if metadata is not None and not self._recovery_metadata_matches(
            lease,
            metadata,
            evidence.payload,
            backup_root=evidence.backup_root,
            reason=reason,
            expected_reservation=None,
        ):
            raise ArchiveError("archive_provenance_mismatch", "archive metadata does not match lease")
        self._validate_archive_parent(evidence.destination)
        return archive.verify_archive(
            evidence.destination,
            expected_metadata=expected_metadata,
            expected_snapshot=evidence.snapshot,
            git=self.git,
            git_state_snapshot=evidence.git_state_snapshot,
        )

    def _read_manifest(
        self, destination: Path
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        self._validate_archive_parent(destination)
        manifest_path = destination / "manifest.json"
        try:
            details = manifest_path.lstat()
        except OSError as error:
            raise ArchiveError("archive_missing", "archive manifest is unavailable") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ArchiveError("archive_invalid", "archive manifest is not a regular file")
        if details.st_size > archive.MAX_MANIFEST_BYTES:
            raise ArchiveError("archive_invalid", "archive manifest exceeds the safe size limit")
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArchiveError("archive_invalid", "archive manifest is malformed") from error
        if not isinstance(parsed, dict):
            raise ArchiveError("archive_invalid", "archive manifest is malformed")
        metadata = parsed.get("metadata")
        snapshot = parsed.get("snapshot")
        git_state_snapshot = parsed.get("git_state_snapshot")
        if (
            not isinstance(metadata, dict)
            or not isinstance(snapshot, dict)
            or (git_state_snapshot is not None and not isinstance(git_state_snapshot, dict))
        ):
            raise ArchiveError("archive_invalid", "archive manifest lacks metadata or snapshot")
        return metadata, snapshot, git_state_snapshot

    def _recovery_metadata_matches(
        self,
        lease: Lease,
        metadata: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        backup_root: Path,
        reason: str,
        expected_reservation: CleanupReservation | None,
    ) -> bool:
        if not self._valid_token_payload(payload):
            return False
        recorded_lease = metadata.get("lease")
        if not isinstance(recorded_lease, Mapping):
            return False
        required_lease_values = {
            "id": lease.id,
            "repository_id": lease.repository_id,
            "repository_name": lease.repository_name,
            "repository_root": str(lease.repository_root),
            "worktree_path": str(lease.worktree_path),
            "branch": lease.branch,
        }
        if any(
            recorded_lease.get(key) != value
            for key, value in required_lease_values.items()
        ):
            return False
        if (
            metadata.get("repository_id") != lease.repository_id
            or metadata.get("reason") != reason
            or metadata.get("preview_token") != self._token(payload)
        ):
            return False
        if (
            payload.get("lease_id") != lease.id
            or payload.get("repository_id") != lease.repository_id
            or payload.get("repository_root") != str(lease.repository_root)
            or payload.get("worktree_path") != str(lease.worktree_path)
            or payload.get("branch") != lease.branch
            or payload.get("backup_root") != str(backup_root)
            or payload.get("reason_sha256") != self._reason_digest(reason)
        ):
            return False
        head_sha = payload["head_sha"]
        if metadata.get("head_sha") != head_sha:
            return False
        closed_promotion_pr = payload["closed_promotion_pr"]
        if self._requires_closed_promotion_pr_evidence(lease):
            if (
                not isinstance(lease.target_pr, int)
                or isinstance(lease.target_pr, bool)
                or lease.target_pr <= 0
                or not isinstance(closed_promotion_pr, Mapping)
                or closed_promotion_pr["number"] != lease.target_pr
                or closed_promotion_pr["head_ref"] != lease.branch
                or closed_promotion_pr["head_sha"] != head_sha
            ):
                return False
        elif closed_promotion_pr is not None:
            return False
        if expected_reservation is not None and (
            expected_reservation.branch_sha != head_sha
            or payload.get("lease_version")
            != expected_reservation.reserved_version - 1
        ):
            return False
        return True

    def _token_payload(
        self,
        lease: Lease,
        *,
        version: int,
        head_sha: str,
        snapshot: Mapping[str, Any],
        git_state_snapshot: Mapping[str, Any] | None,
        include_uncommitted: bool,
        exclude_ignored_paths: tuple[str, ...],
        remote_sha: str | None,
        closed_promotion_pr: dict[str, Any] | None,
        backup_root: Path,
        reason: str,
    ) -> dict[str, Any]:
        payload = {
            "schema": "awf.archive-discard/v2",
            "lease_id": lease.id,
            "lease_version": version,
            "repository_id": lease.repository_id,
            "repository_name": lease.repository_name,
            "repository_root": str(lease.repository_root),
            "worktree_path": str(lease.worktree_path),
            "branch": lease.branch,
            "head_sha": head_sha,
            "snapshot_fingerprint": snapshot["fingerprint"],
            "snapshot_entry_count": len(snapshot["entries"]),
            "snapshot_total_bytes": snapshot["total_bytes"],
            "snapshot": snapshot,
            "remote_sha": remote_sha,
            "open_prs": [],
            "closed_promotion_pr": closed_promotion_pr,
            "backup_root": str(backup_root),
            "reason_sha256": self._reason_digest(reason),
        }
        if git_state_snapshot is None:
            if include_uncommitted or exclude_ignored_paths:
                raise ValueError("expanded archive discard requires a Git state snapshot")
            return payload
        return {
            **payload,
            "schema": "awf.archive-discard/v3",
            "include_uncommitted": include_uncommitted,
            "exclude_ignored_paths": list(exclude_ignored_paths),
            "git_state_fingerprint": git_state_snapshot["fingerprint"],
        }

    def _valid_token_payload(self, payload: Mapping[str, Any]) -> bool:
        common_keys = {
            "schema",
            "lease_id",
            "lease_version",
            "repository_id",
            "repository_name",
            "repository_root",
            "worktree_path",
            "branch",
            "head_sha",
            "snapshot_fingerprint",
            "snapshot_entry_count",
            "snapshot_total_bytes",
            "snapshot",
            "remote_sha",
            "open_prs",
            "closed_promotion_pr",
            "backup_root",
            "reason_sha256",
        }
        schema = payload.get("schema")
        if schema == "awf.archive-discard/v3":
            expected_keys = common_keys | {
                "include_uncommitted",
                "exclude_ignored_paths",
                "git_state_fingerprint",
            }
        elif schema == "awf.archive-discard/v2":
            expected_keys = common_keys
        else:
            return False
        if set(payload) != expected_keys:
            return False
        if any(
            not isinstance(payload[key], str) or not payload[key]
            for key in (
                "lease_id",
                "repository_id",
                "repository_name",
                "repository_root",
                "worktree_path",
                "branch",
                "backup_root",
            )
        ):
            return False
        if (
            not isinstance(payload["lease_version"], int)
            or isinstance(payload["lease_version"], bool)
            or payload["lease_version"] < 0
            or not isinstance(payload["head_sha"], str)
            or _GIT_OBJECT_ID.fullmatch(payload["head_sha"]) is None
            or not isinstance(payload["reason_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["reason_sha256"]) is None
        ):
            return False
        snapshot = payload["snapshot"]
        if not isinstance(snapshot, Mapping) or not self._valid_snapshot(snapshot):
            return False
        if (
            payload["snapshot_fingerprint"] != snapshot["fingerprint"]
            or not isinstance(payload["snapshot_entry_count"], int)
            or isinstance(payload["snapshot_entry_count"], bool)
            or payload["snapshot_entry_count"] != len(snapshot["entries"])
            or payload["snapshot_total_bytes"] != snapshot["total_bytes"]
        ):
            return False
        remote_sha = payload["remote_sha"]
        if remote_sha is not None and (
            not isinstance(remote_sha, str) or _GIT_OBJECT_ID.fullmatch(remote_sha) is None
        ):
            return False
        if payload["open_prs"] != [] or not isinstance(payload["open_prs"], list):
            return False
        if schema == "awf.archive-discard/v3":
            try:
                excluded_paths = self._canonical_exclude_ignored_paths(
                    tuple(payload["exclude_ignored_paths"])
                )
                snapshot_excluded_paths = self._snapshot_excluded_paths(snapshot)
            except (TypeError, ValueError):
                return False
            if (
                not isinstance(payload["include_uncommitted"], bool)
                or list(excluded_paths) != payload["exclude_ignored_paths"]
                or excluded_paths != snapshot_excluded_paths
                or not isinstance(payload["git_state_fingerprint"], str)
                or re.fullmatch(r"[0-9a-f]{64}", payload["git_state_fingerprint"]) is None
            ):
                return False
        closed_promotion_pr = payload["closed_promotion_pr"]
        if closed_promotion_pr is None:
            return True
        if not isinstance(closed_promotion_pr, Mapping) or set(closed_promotion_pr) != {
            "number",
            "state",
            "head_ref",
            "head_sha",
            "merge_commit_sha",
            "repository_sha256",
        }:
            return False
        return (
            isinstance(closed_promotion_pr["number"], int)
            and not isinstance(closed_promotion_pr["number"], bool)
            and closed_promotion_pr["number"] > 0
            and closed_promotion_pr["state"] == "CLOSED"
            and isinstance(closed_promotion_pr["head_ref"], str)
            and bool(closed_promotion_pr["head_ref"])
            and isinstance(closed_promotion_pr["head_sha"], str)
            and _GIT_OBJECT_ID.fullmatch(closed_promotion_pr["head_sha"]) is not None
            and closed_promotion_pr["merge_commit_sha"] is None
            and isinstance(closed_promotion_pr["repository_sha256"], str)
            and re.fullmatch(
                r"[0-9a-f]{64}", closed_promotion_pr["repository_sha256"]
            )
            is not None
        )

    @staticmethod
    def _token(payload: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _tokens_match(provided: str, expected: str) -> bool:
        return hmac.compare_digest(provided, expected)

    @staticmethod
    def _reason_digest(reason: str) -> str:
        return hashlib.sha256(reason.encode("utf-8")).hexdigest()


    @staticmethod
    def _valid_snapshot(snapshot: Mapping[str, Any]) -> bool:
        return (
            isinstance(snapshot.get("fingerprint"), str)
            and re.fullmatch(r"[0-9a-f]{64}", snapshot["fingerprint"]) is not None
            and isinstance(snapshot.get("entries"), list)
            and isinstance(snapshot.get("total_bytes"), int)
            and not isinstance(snapshot.get("total_bytes"), bool)
            and snapshot["total_bytes"] >= 0
        )

    @staticmethod
    def _canonical_exclude_ignored_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
        canonical: set[str] = set()
        for raw_path in paths:
            if not isinstance(raw_path, str) or not raw_path or "\0" in raw_path:
                raise ValueError("excluded paths must be non-empty strings")
            path = PurePosixPath(raw_path)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError("excluded paths must be normalized and relative")
            normalized = str(path)
            if normalized in {"", "."}:
                raise ValueError("excluded paths must be normalized and relative")
            canonical.add(normalized)
        return tuple(sorted(canonical))

    @classmethod
    def _snapshot_excluded_paths(cls, snapshot: Mapping[str, Any]) -> tuple[str, ...]:
        recorded = snapshot.get("excluded_paths")
        if recorded is None:
            return ()
        if not isinstance(recorded, list):
            raise ValueError("snapshot exclusion policy must be a list")
        canonical = cls._canonical_exclude_ignored_paths(tuple(recorded))
        if list(canonical) != recorded:
            raise ValueError("snapshot exclusion policy is not canonical")
        return canonical

    @staticmethod
    def _valid_git_state_snapshot(snapshot: object) -> bool:
        return (
            isinstance(snapshot, Mapping)
            and isinstance(snapshot.get("fingerprint"), str)
            and re.fullmatch(r"[0-9a-f]{64}", snapshot["fingerprint"]) is not None
        )

    @staticmethod
    def _payload_uses_git_state(payload: Mapping[str, Any]) -> bool:
        return payload.get("schema") == "awf.archive-discard/v3"

    @classmethod
    def _payload_policy(cls, payload: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
        if not cls._payload_uses_git_state(payload):
            return False, ()
        return (
            payload["include_uncommitted"],
            tuple(payload["exclude_ignored_paths"]),
        )

    def _checkpoint_git_state(
        self,
        manifest: Mapping[str, Any],
        expected_snapshot: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if expected_snapshot is None:
            return None
        manifest_snapshot = manifest.get("git_state_snapshot")
        if (
            not self._valid_git_state_snapshot(manifest_snapshot)
            or manifest_snapshot != expected_snapshot
        ):
            raise ArchiveError(
                "archive_provenance_mismatch",
                "archive Git state snapshot does not match the token evidence",
            )
        records = manifest.get("git_state_artifacts")
        artifacts = manifest.get("artifacts")
        required_names = {"git-state.tar", "git-objects.pack"}
        if (
            not isinstance(records, Mapping)
            or set(records) != required_names
            or not isinstance(artifacts, Mapping)
            or any(artifacts.get(name) != records[name] for name in required_names)
        ):
            raise ArchiveError(
                "archive_corrupt", "archive Git state artifacts are incomplete"
            )
        normalized: dict[str, Any] = {}
        for name in sorted(required_names):
            record = records[name]
            if (
                not isinstance(record, Mapping)
                or set(record) != {"bytes", "sha256"}
                or not isinstance(record["bytes"], int)
                or isinstance(record["bytes"], bool)
                or record["bytes"] < 0
                or not isinstance(record["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
            ):
                raise ArchiveError(
                    "archive_corrupt", "archive Git state artifact records are invalid"
                )
            normalized[name] = {
                "bytes": record["bytes"],
                "sha256": record["sha256"],
            }
        return normalized

    def _recover_reserved_checkpoint(self, lease: Lease, evidence: _Evidence) -> None:
        if evidence.git_state_snapshot is None:
            return
        if evidence.git_state_artifacts is None:
            raise ArchiveError(
                "archive_provenance_mismatch",
                "archive Git state checkpoint records are unavailable",
            )
        recover_archived_removal_checkpoint(
            self.git,
            lease.worktree_path,
            git_state=evidence.git_state_artifacts,
            private_root=evidence.backup_root,
        )


    def _archive_destination(self, backup_root: Path, lease: Lease, token: str) -> Path:
        return backup_root / lease.repository_id / lease.id / token

    def _prepare_archive_parent(self, evidence: _Evidence) -> None:
        root = self._validate_backup_root(evidence.backup_root)
        if root != evidence.backup_root:
            raise ArchiveError("backup_root_invalid", "backup root changed during archive discard")
        try:
            relative = evidence.destination.relative_to(root)
        except ValueError as error:
            raise ArchiveError(
                "archive_destination_invalid", "archive destination escapes the backup root"
            ) from error
        parent = root
        for component in relative.parts[:-1]:
            candidate = parent / component
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                try:
                    candidate.mkdir(mode=0o700)
                except FileExistsError:
                    details = candidate.lstat()
                except OSError as error:
                    raise ArchiveError(
                        "archive_create_failed", "unable to create archive parent"
                    ) from error
                else:
                    details = candidate.lstat()
                    self._fsync_directory(parent)
            except OSError as error:
                raise ArchiveError(
                    "archive_unsafe", "unable to inspect archive parent"
                ) from error
            self._require_private_directory(details)
            parent = candidate
        validated_parent = archive.validate_backup_root(parent, forbidden_roots=())
        if validated_parent != parent:
            raise ArchiveError("archive_unsafe", "archive parent changed during validation")

    def _validate_archive_parent(self, destination: Path) -> None:
        try:
            parent = destination.parent
            validated = archive.validate_backup_root(parent, forbidden_roots=())
        except ArchiveError:
            raise
        except OSError as error:
            raise ArchiveError("archive_unsafe", "archive parent is unavailable") from error
        if validated != parent:
            raise ArchiveError("archive_unsafe", "archive parent is not canonical")

    @staticmethod
    def _require_private_directory(details: os.stat_result) -> None:
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise ArchiveError("archive_unsafe", "archive parent is not private")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ArchiveError("archive_write_failed", "unable to open archive parent") from error
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise ArchiveError("archive_write_failed", "unable to sync archive parent") from error
        finally:
            os.close(descriptor)

    def _archive_action(
        self, kind: str, lease: Lease, evidence: _Evidence
    ) -> dict[str, object]:
        return {
            **self._action(kind, lease, evidence.head_sha),
            "preview_token": evidence.token,
            "backup_directory": str(evidence.destination),
            "snapshot": {
                "fingerprint": evidence.snapshot["fingerprint"],
                "file_count": len(evidence.snapshot["entries"]),
                "total_bytes": evidence.snapshot["total_bytes"],
            },
        }

    @staticmethod
    def _action(kind: str, lease: Lease, head_sha: str) -> dict[str, object]:
        return {
            "kind": kind,
            "lease_id": lease.id,
            "path": str(lease.worktree_path),
            "branch": lease.branch,
            "head_sha": head_sha,
        }

    @staticmethod
    def _is_sync_lease(lease: Lease) -> bool:
        return _SYNC_BRANCH.fullmatch(lease.branch) is not None

    @staticmethod
    def _requires_closed_promotion_pr_evidence(lease: Lease) -> bool:
        return (
            lease.purpose is Purpose.PROMOTE
            and lease.resolution_state
            in {ResolutionState.AUTOMATIC, ResolutionState.MANUAL_REVIEWED}
            and not (
                lease.state is LeaseState.BLOCKED
                and lease.resolution_state is ResolutionState.MANUAL_REVIEWED
                and lease.target_pr is None
            )
        )

    @staticmethod
    def _is_awf_candidate(lease: Lease) -> bool:
        return lease.managed and lease.owner_kind == "awf"

    @classmethod
    def _is_imported_candidate(cls, lease: Lease) -> bool:
        return cls._is_imported_scratch_candidate(lease) and lease.state is LeaseState.ACTIVE

    @staticmethod
    def _is_imported_scratch_candidate(lease: Lease) -> bool:
        return (
            lease.owner_kind == "imported"
            and not lease.managed
            and lease.purpose is Purpose.SCRATCH
            and lease.state in {LeaseState.ACTIVE, LeaseState.DIRTY}
        )

    @classmethod
    def _is_dirty_imported_candidate(cls, lease: Lease) -> bool:
        return cls._is_imported_scratch_candidate(lease) and lease.state is LeaseState.DIRTY

    @staticmethod
    def _is_blocked_manual_candidate(lease: Lease, legacy_retry: bool) -> bool:
        return (
            lease.state is LeaseState.BLOCKED
            and (
                legacy_retry
                or (
                    lease.managed
                    and lease.owner_kind == "awf"
                    and lease.purpose is Purpose.PROMOTE
                )
            )
        )

    def _is_legacy_promotion_retry_path(self, lease: Lease) -> bool:
        target_base_sha = lease.target_base_sha
        if (
            not lease.managed
            or lease.owner_kind != "awf"
            or lease.purpose is not Purpose.PROMOTE
            or lease.promotion_mode is not PromotionMode.OUT_OF_ORDER
            or target_base_sha is None
            or _GIT_OBJECT_ID.fullmatch(target_base_sha) is None
        ):
            return False
        suffix = f"-retry-{target_base_sha}"
        if not lease.initiative.endswith(suffix):
            return False
        initiative = lease.initiative.removesuffix(suffix)
        if (
            not initiative
            or not initiative.isascii()
            or initiative[0] == "-"
            or initiative[-1] == "-"
            or "--" in initiative
            or not all(
                character.islower() or character.isdigit() or character == "-"
                for character in initiative
            )
        ):
            return False
        retry_initiative = f"{initiative}-retry-{target_base_sha}"
        digest = hashlib.sha256(retry_initiative.encode("utf-8")).hexdigest()[:16]
        return (
            lease.initiative == retry_initiative
            and lease.branch == f"awf/{retry_initiative}/promote"
            and lease.worktree_path
            == self.cache_dir
            / lease.repository_name
            / f"promotion-retry-{target_base_sha}-{digest}"
        )

    def _deletes_local_branch(self, lease: Lease) -> bool:
        return self._is_awf_candidate(lease) and lease.branch.startswith("awf/")

    def _protected_branches(
        self, worktrees: tuple[GitWorktree, ...], repository_root: Path, lease: Lease
    ) -> set[str]:
        protected = {
            self._local_branch_name(self.default_base),
            self._local_branch_name(self.production_branch),
            self._local_branch_name(self.git.default_remote_branch()),
        }
        if not self._is_imported_scratch_candidate(lease):
            protected.add(self._local_branch_name(lease.base_ref))
        protected.update(
            item.branch
            for item in worktrees
            if self._same_path(item.path, repository_root) and item.branch is not None
        )
        protected.discard("")
        return protected

    def _path_absent(self, lease: Lease) -> bool:
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError):
            return False
        return not lease.worktree_path.exists() and not any(
            self._same_path(item.path, lease.worktree_path) for item in worktrees
        )

    def _release_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        message: str,
        *,
        code: str = "post_backup_drift",
    ) -> CommandResult:
        try:
            released = self.registry.release_cleanup_reservation(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, sqlite3.Error):
            return self._reserved_blocked(
                lease, "The cleanup reservation was retained because it could not be released safely."
            )
        return self._blocked(code, message, lease=released)

    def _reserved_blocked(self, lease: Lease, message: str) -> CommandResult:
        return self._blocked("cleanup_reserved", message, lease=lease)

    def _reserved_archive_blocked(self, lease: Lease, error: ArchiveError) -> CommandResult:
        return self._blocked(
            "cleanup_reserved",
            f"The cleanup reservation was retained because archive verification failed ({error.code}).",
            lease=lease,
        )

    @staticmethod
    def _archive_blocked(error: ArchiveError, lease: Lease) -> CommandResult:
        return CommandResult.blocked(
            _COMMAND,
            blockers=({"code": error.code, "message": "Archive backup validation failed."},),
            lease=lease,
        )

    @staticmethod
    def _blocked(code: str, message: str, *, lease: Lease | None = None) -> CommandResult:
        return CommandResult.blocked(
            _COMMAND, blockers=({"code": code, "message": message},), lease=lease
        )

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _canonical_uuid(value: str) -> bool:
        try:
            import uuid

            return str(uuid.UUID(value)) == value
        except (AttributeError, ValueError):
            return False

    @staticmethod
    def _path_has_no_symlink_ancestors(path: Path) -> bool:
        current = Path(path.anchor)
        try:
            for part in path.parts[1:]:
                current /= part
                details = current.lstat()
                if stat.S_ISLNK(details.st_mode):
                    return False
        except OSError:
            return False
        return True

    @staticmethod
    def _local_branch_name(value: str | None) -> str:
        if not value:
            return ""
        for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
            if value.startswith(prefix):
                return value[len(prefix) :]
        return value
