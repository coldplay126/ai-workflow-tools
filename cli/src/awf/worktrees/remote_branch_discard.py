from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import archive
from .git import GitClient, GitError, GitRemoteError, _normalize_remote_url
from .github import ExternalServiceError, GhClient
from .locking import repository_lock
from .models import CommandResult, Lease, LeaseState, ReleaseState, now_iso
from .registry import WorktreeRegistry


_COMMAND = "wt.discard-remote-branch"
_TOKEN_SCHEMA = "awf.discard-remote-branch/v1"
_MANIFEST_SCHEMA = "awf.discard-remote-branch-manifest/v1"
_ATTEMPT_SCHEMA = "awf.discard-remote-branch-attempt/v1"
_RECEIPT_SCHEMA = "awf.discard-remote-branch-receipt/v1"
_NAMESPACE = "remote-branch-discard"
_BACKUP_KIND = "full_history_commit_bundle"
_INTEGRATION_BRANCHES = frozenset(
    {"main", "master", "staging", "production", "develop", "trunk"}
)
_PROTECTED_PREFIXES = ("release/", "release-archive-", "staging-archive-")
_INACTIVE_RELEASE_STATES = frozenset(
    {ReleaseState.MERGED, ReleaseState.CLOSED_UNMERGED, ReleaseState.CLEANED}
)
_UNSAFE_GIT_ENVIRONMENT_NAMES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)
_UNSAFE_GIT_ENVIRONMENT_PREFIXES = ("GIT_CONFIG_",)
_MAX_JSON_BYTES = 1024 * 1024
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_TOKEN = re.compile(r"[0-9a-f]{64}")
_DEFINITE_PRECONNECT_FAILURES = (
    "could not resolve host",
    "could not resolve hostname",
    "authentication failed for",
    "permission denied (publickey)",
)


@dataclass(frozen=True)
class _Evidence:
    token: str
    payload: dict[str, Any]
    branch: str
    expected_sha: str
    observed_remote_sha: str | None
    reason: str
    backup_root: Path
    destination: Path
    leases: tuple[Lease, ...]


@dataclass(frozen=True)
class _Backup:
    artifact: dict[str, Any]
    attempt: dict[str, Any] | None
    receipt: dict[str, Any] | None


class RemoteBranchDiscarder:
    """Back up one approved origin ref before deleting it with a remote CAS."""
    _command = _COMMAND
    _token_schema = _TOKEN_SCHEMA
    _manifest_schema = _MANIFEST_SCHEMA
    _attempt_schema = _ATTEMPT_SCHEMA
    _receipt_schema = _RECEIPT_SCHEMA
    _namespace = _NAMESPACE
    _sha_key = "remote_sha"
    _delete_action_kind = "delete_remote_branch"
    _branch_subject = "remote branch"
    _discard_subject = "remote discard"
    _bundle_source = "origin branch"
    _absent_completion = "remote_absent_on_retry"
    _valid_completions = frozenset({"deleted", "remote_absent_on_retry"})
    _operation_label = "remote discard"




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
        feature_base: str | None,
    ) -> None:
        self.registry = registry
        self.git = git
        self.github = github
        self.cache_dir = Path(cache_dir)
        self.lock_dir = Path(lock_dir)
        self.default_base = default_base
        self.production_branch = production_branch
        self.feature_base = feature_base

    def run(
        self,
        branch: str,
        *,
        expected_sha: str,
        backup_root: Path,
        reason: str,
        preview_token: str | None,
        apply: bool,
    ) -> CommandResult:
        environment_blocker = self._unsafe_git_environment_blocker()
        if environment_blocker is not None:
            return environment_blocker
        argument_blocker = self._argument_blocker(
            branch, expected_sha, backup_root, reason, preview_token, apply
        )
        if argument_blocker is not None:
            return argument_blocker
        try:
            self.git.validate_branch_name(branch)
        except (GitError, OSError):
            return self._blocked(
                "invalid_branch", "branch is not a valid Git branch name"
            )
        try:
            lock_repository_id = self.git.repository_id()
        except (GitError, OSError):
            return self._blocked(
                "repository_inspection_failed", "Unable to inspect the current Git repository."
            )

        if not apply:
            inspected = self._inspect(
                branch,
                expected_sha=expected_sha,
                backup_root=backup_root,
                reason=reason,
            )
            if isinstance(inspected, CommandResult):
                return inspected
            return self._preview(inspected)

        assert preview_token is not None
        with repository_lock(self.lock_dir / f"{lock_repository_id}.lock"):
            inspected = self._inspect(
                branch,
                expected_sha=expected_sha,
                backup_root=backup_root,
                reason=reason,
            )
            if isinstance(inspected, CommandResult):
                return inspected
            if not self._tokens_match(preview_token, inspected.token):
                return self._blocked(
                    "preview_token_mismatch",
                    "The preview token no longer matches the current discard evidence.",
                    leases=inspected.leases,
                )
            return self._apply(inspected, preview_token)

    def _argument_blocker(
        self,
        branch: str,
        expected_sha: str,
        backup_root: Path,
        reason: str,
        preview_token: str | None,
        apply: bool,
    ) -> CommandResult | None:
        if not isinstance(branch, str) or not branch:
            return self._blocked("invalid_branch", "branch must be a non-empty string")
        if not isinstance(expected_sha, str) or _GIT_OBJECT_ID.fullmatch(expected_sha) is None:
            return self._blocked(
                "invalid_expected_sha", "expected_sha must be a lowercase Git object identifier"
            )
        if not isinstance(backup_root, Path) or not backup_root.is_absolute():
            return self._blocked(
                "invalid_backup_root", "backup_root must be an absolute path"
            )
        if not isinstance(reason, str) or not reason.strip():
            return self._blocked("invalid_reason", "reason must be a non-empty string")
        if not isinstance(apply, bool):
            return self._blocked("invalid_apply", "apply must be a boolean")
        if apply and (
            not isinstance(preview_token, str) or _TOKEN.fullmatch(preview_token) is None
        ):
            return self._blocked(
                "preview_token_required",
                "apply requires the exact preview token returned by a current preview",
            )
        return None

    def _unsafe_git_environment_blocker(self) -> CommandResult | None:
        unsafe = sorted(
            name
            for name in os.environ
            if name in _UNSAFE_GIT_ENVIRONMENT_NAMES
            or name.startswith(_UNSAFE_GIT_ENVIRONMENT_PREFIXES)
        )
        if unsafe:
            return self._blocked(
                "unsafe_git_environment",
                "Git repository binding environment variables are not allowed for this command.",
            )
        return None

    def _inspect(
        self,
        branch: str,
        *,
        expected_sha: str,
        backup_root: Path,
        reason: str,
    ) -> _Evidence | CommandResult:
        environment_blocker = self._unsafe_git_environment_blocker()
        if environment_blocker is not None:
            return environment_blocker
        try:
            repository_root = self.git.common_repository_root()
            repository_id = self.git.repository_id()
        except (GitError, OSError):
            return self._blocked(
                "repository_inspection_failed", "Unable to inspect the current Git repository."
            )
        try:
            leases = tuple(self.registry.list_leases_read_only(include_removed=True))
        except sqlite3.Error:
            return self._blocked(
                "registry_conflict", "Unable to inspect the worktree registry."
            )
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError):
            return self._blocked(
                "worktree_inspection_failed", "Unable to inspect registered Git worktrees."
            )
        try:
            validated_backup_root = archive.validate_backup_root(
                backup_root,
                forbidden_roots=(
                    repository_root,
                    self.cache_dir,
                    *(lease.worktree_path for lease in leases),
                    *(item.path for item in worktrees),
                ),
            )
        except archive.ArchiveError as error:
            return self._archive_blocked(error)
        except (OSError, RuntimeError, ValueError):
            return self._blocked(
                "backup_root_invalid", "The backup root could not be safely validated."
            )
        foreign_lease = self._foreign_remote_lease_blocker(
            branch, leases=leases, repository_id=repository_id
        )
        if foreign_lease is not None:
            return foreign_lease
        try:
            default_remote_branch = self.git.default_remote_branch()
        except (GitError, OSError):
            return self._blocked(
                "default_branch_unknown", "Unable to determine origin's default branch."
            )
        protected = self._protected_branch_blocker(
            branch, default_remote_branch=default_remote_branch, leases=leases, repository_id=repository_id
        )
        if protected is not None:
            return protected
        if any(item.branch == branch for item in worktrees):
            return self._blocked(
                "live_worktree",
                "A registered Git worktree currently checks out this branch.",
            )
        matching_leases = tuple(
            sorted(
                (
                    lease
                    for lease in leases
                    if lease.repository_id == repository_id and lease.branch == branch
                ),
                key=lambda lease: lease.id,
            )
        )
        lease_blocker = self._lease_blocker(matching_leases, repository_id=repository_id)
        if lease_blocker is not None:
            return lease_blocker
        try:
            local_commit = self.git.resolve_ref(f"{expected_sha}^{{commit}}")
        except (GitError, OSError):
            return self._blocked(
                "remote_commit_missing_locally",
                "The approved remote commit is unavailable as a local commit object.",
                leases=matching_leases,
            )
        if local_commit != expected_sha:
            return self._blocked(
                "remote_commit_missing_locally",
                "The approved remote commit is unavailable as a local commit object.",
                leases=matching_leases,
            )
        try:
            remote_repository = self.git.remote_url()
            push_remote_urls = tuple(
                self.git._run(
                    "remote", "get-url", "--push", "--all", "origin"
                )
                .stdout.decode("utf-8", errors="replace")
                .splitlines()
            )
        except (GitError, OSError, ValueError):
            return self._blocked(
                "preflight_inspection_failed",
                "Unable to inspect remote repository metadata for the branch.",
                leases=matching_leases,
            )
        if len(push_remote_urls) != 1 or push_remote_urls[0] != remote_repository:
            return self._blocked(
                "push_remote_mismatch",
                "Origin's resolved push target must be its single resolved fetch target.",
                leases=matching_leases,
            )
        try:
            github = self.github or GhClient(repository_root)
            open_pull_requests = github.find_open_prs(
                head=branch, repository=remote_repository
            )
        except ExternalServiceError:
            return self._external_error(
                "github_refresh_failed",
                "Unable to inspect open pull requests for the branch.",
                leases=matching_leases,
            )
        except (GitError, OSError, ValueError):
            return self._blocked(
                "preflight_inspection_failed",
                "Unable to inspect remote repository metadata for the branch.",
                leases=matching_leases,
            )
        if open_pull_requests:
            return self._blocked(
                "open_pull_request",
                "The branch has an open pull request and cannot be discarded.",
                leases=matching_leases,
            )
        try:
            observed_remote_sha = self.git.remote_branch_sha(branch)
        except GitRemoteError:
            return self._external_error(
                "remote_branch_inspection_failed",
                "Unable to inspect the remote branch.",
                leases=matching_leases,
            )
        except (GitError, OSError):
            return self._blocked(
                "preflight_inspection_failed",
                "Unable to inspect the remote branch.",
                leases=matching_leases,
            )
        payload = self._token_payload(
            repository_id=repository_id,
            repository_root=repository_root,
            origin_url_sha256=hashlib.sha256(
                remote_repository.encode("utf-8")
            ).hexdigest(),
            branch=branch,
            expected_sha=expected_sha,
            backup_root=validated_backup_root,
            reason=reason,
            leases=matching_leases,
        )
        token = self._token(payload)
        return _Evidence(
            token=token,
            payload=payload,
            branch=branch,
            expected_sha=expected_sha,
            observed_remote_sha=observed_remote_sha,
            reason=reason,
            backup_root=validated_backup_root,
            destination=validated_backup_root / repository_id / self._namespace / token,
            leases=matching_leases,
        )

    def _foreign_remote_lease_blocker(
        self, branch: str, *, leases: tuple[Lease, ...], repository_id: str
    ) -> CommandResult | None:
        """A shared origin must not let a different repository identity bypass a lease."""
        candidates = (
            lease for lease in leases
            if lease.repository_id != repository_id
            and (
                lease.branch == branch
                or (
                    lease.state is not LeaseState.REMOVED
                    and self._strip_refs(lease.base_ref) == branch
                )
            )
        )
        try:
            remote = _normalize_remote_url(self.git.remote_url())
            for lease in candidates:
                if _normalize_remote_url(GitClient(lease.repository_root).remote_url()) == remote:
                    return self._blocked(
                        "repository_mismatch",
                        "A lease for this origin has a different repository identity.",
                    )
        except (GitError, OSError):
            return self._blocked(
                "repository_inspection_failed",
                "Unable to verify the repository identity of a relevant lease.",
            )
        return None

    def _protected_branch_blocker(
        self,
        branch: str,
        *,
        default_remote_branch: str,
        leases: tuple[Lease, ...],
        repository_id: str,
    ) -> CommandResult | None:
        protected = {
            self._strip_refs(self.default_base),
            self._strip_refs(self.production_branch),
            self._strip_refs(self.feature_base),
            self._strip_refs(default_remote_branch),
            *_INTEGRATION_BRANCHES,
        }
        protected.update(
            self._strip_refs(lease.base_ref)
            for lease in leases
            if lease.repository_id == repository_id and lease.state is not LeaseState.REMOVED
        )
        protected.discard("")
        if branch in protected or branch.startswith(_PROTECTED_PREFIXES):
            return self._blocked(
                "protected_branch", f"The branch is protected from {self._operation_label}."
            )
        return None

    def _lease_blocker(
        self, leases: tuple[Lease, ...], *, repository_id: str
    ) -> CommandResult | None:
        try:
            if any(
                self.registry.get_cleanup_reservation(lease.id) is not None
                for lease in leases
            ):
                return self._blocked(
                    "cleanup_reserved",
                    "A matching worktree lease is reserved for cleanup.",
                    leases=leases,
                )
        except sqlite3.Error:
            return self._blocked(
                "registry_conflict", "Unable to inspect worktree registry guards.", leases=leases
            )
        if any(lease.state is not LeaseState.REMOVED for lease in leases):
            return self._blocked(
                "lease_not_removed",
                "A matching worktree lease has not been removed.",
                leases=leases,
            )
        if any(lease.retain for lease in leases):
            return self._blocked(
                "retained_lease",
                "A matching worktree lease is marked for retention.",
                leases=leases,
            )
        try:
            for lease in leases:
                if not lease.initiative.startswith("release-"):
                    continue
                release = self.registry.find_release_read_only(
                    repository_id, lease.initiative[len("release-") :]
                )
                if release is not None and (
                    release.state not in _INACTIVE_RELEASE_STATES
                    or release.target_branch == lease.branch
                ):
                    return self._blocked(
                        "active_release",
                        "The branch is associated with an active release.",
                        leases=leases,
                    )
        except sqlite3.Error:
            return self._blocked(
                "registry_conflict", "Unable to inspect worktree registry guards.", leases=leases
            )
        return None

    def _preview(self, evidence: _Evidence) -> CommandResult:
        try:
            backup = self._read_existing_backup(evidence)
        except archive.ArchiveError as error:
            return self._archive_blocked(error, leases=evidence.leases)
        except GitError:
            return self._archive_corrupt(evidence)
        if backup is not None and backup.receipt is not None:
            return self._completed(evidence, backup, idempotent=True)
        if backup is not None and backup.attempt is not None:
            if evidence.observed_remote_sha is not None:
                return self._blocked(
                    "remote_delete_outcome_unknown",
                    "A previous remote deletion attempt has no completion receipt.",
                    leases=evidence.leases,
                    actions=self._backup_actions(evidence, backup.artifact),
                )
            return self._completion_preview(evidence, backup)
        if evidence.observed_remote_sha is None:
            return self._blocked(
                "remote_branch_absent",
                "The remote branch is absent without a prior deletion attempt.",
                leases=evidence.leases,
            )
        if evidence.observed_remote_sha != evidence.expected_sha:
            return self._blocked(
                "remote_head_mismatch",
                "The remote branch no longer matches the approved commit.",
                leases=evidence.leases,
            )
        return CommandResult.ok(
            self._command,
            decision="preview",
            actions=self._preview_actions(evidence),
        )

    def _apply(self, evidence: _Evidence, preview_token: str) -> CommandResult:
        try:
            backup = self._read_existing_backup(evidence)
        except archive.ArchiveError as error:
            return self._archive_blocked(error, leases=evidence.leases)
        except GitError:
            return self._archive_corrupt(evidence)
        if backup is not None and backup.receipt is not None:
            return self._completed(evidence, backup, idempotent=True)
        if backup is not None and backup.attempt is not None:
            return self._resolve_attempt(evidence, backup)
        if evidence.observed_remote_sha is None:
            return self._blocked(
                "remote_branch_absent",
                "The remote branch is absent without a prior deletion attempt.",
                leases=evidence.leases,
            )
        if evidence.observed_remote_sha != evidence.expected_sha:
            return self._blocked(
                "remote_head_changed",
                "The remote branch no longer matches the approved commit.",
                leases=evidence.leases,
            )
        try:
            backup = self._create_or_verify_backup(evidence)
        except archive.ArchiveError as error:
            return self._archive_blocked(error, leases=evidence.leases)
        except GitError:
            return self._blocked(
                "backup_verification_failed",
                "The commit bundle could not be independently verified.",
                leases=evidence.leases,
            )
        if backup.receipt is not None:
            return self._completed(evidence, backup, idempotent=True)
        if backup.attempt is not None:
            return self._resolve_attempt(evidence, backup)

        revalidated = self._inspect(
            evidence.branch,
            expected_sha=evidence.expected_sha,
            backup_root=evidence.backup_root,
            reason=evidence.reason,
        )
        if isinstance(revalidated, CommandResult):
            return revalidated
        if not self._tokens_match(preview_token, revalidated.token) or not self._tokens_match(
            evidence.token, revalidated.token
        ):
            return self._blocked(
                "preview_token_mismatch",
                "The preview token no longer matches the current discard evidence.",
                leases=revalidated.leases,
                actions=self._backup_actions(evidence, backup.artifact),
            )
        if revalidated.observed_remote_sha is None or (
            revalidated.observed_remote_sha != revalidated.expected_sha
        ):
            return self._blocked(
                "remote_head_changed",
                "The remote branch changed before deletion.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, backup.artifact),
            )
        try:
            revalidated_backup = self._read_existing_backup(revalidated)
        except archive.ArchiveError as error:
            return self._archive_blocked(error, leases=revalidated.leases)
        except GitError:
            return self._archive_corrupt(revalidated)
        if revalidated_backup is None:
            return self._archive_corrupt(revalidated)
        if revalidated_backup.receipt is not None:
            return self._completed(revalidated, revalidated_backup, idempotent=True)
        if revalidated_backup.attempt is not None:
            return self._resolve_attempt(revalidated, revalidated_backup)
        try:
            self._write_attempt(revalidated)
        except archive.ArchiveError as error:
            return self._archive_blocked(
                error,
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        try:
            self.git.delete_remote_branch_if_at(
                revalidated.branch, revalidated.expected_sha, skip_hooks=True
            )
        except GitRemoteError as error:
            if any(marker in str(error).lower() for marker in _DEFINITE_PRECONNECT_FAILURES):
                try:
                    self._clear_failed_attempt(revalidated)
                except (archive.ArchiveError, OSError):
                    return self._blocked(
                        "remote_delete_outcome_unknown",
                        "The confirmed transport failure could not be recorded safely.",
                        leases=revalidated.leases,
                        actions=self._backup_actions(revalidated, revalidated_backup.artifact),
                    )
                return self._external_error(
                    "remote_transport_failed",
                    "The connection failed before the deletion request; retry this token after restoring connectivity.",
                    leases=revalidated.leases,
                    actions=self._backup_actions(revalidated, revalidated_backup.artifact),
                )
            return self._external_error(
                "remote_delete_failed",
                "The remote deletion request failed during transport; its outcome is unknown. Do not retry this token while the branch exists.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        except GitError:
            try:
                self._clear_failed_attempt(revalidated)
            except (archive.ArchiveError, OSError):
                return self._blocked(
                    "remote_delete_outcome_unknown",
                    "The confirmed CAS rejection could not be recorded safely.",
                    leases=revalidated.leases,
                    actions=self._backup_actions(revalidated, revalidated_backup.artifact),
                )
            return self._blocked(
                "remote_head_changed",
                "The remote branch changed before its deletion could be committed.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        try:
            post_delete_remote_sha = self.git.remote_branch_sha(revalidated.branch)
        except GitRemoteError:
            return self._external_error(
                "remote_delete_unverified",
                "The remote deletion completed but could not be independently verified.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        if post_delete_remote_sha is not None:
            return self._external_error(
                "remote_delete_unverified",
                "The remote branch still exists after the deletion request.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        try:
            completed_backup = self._write_receipt(revalidated, completion="deleted")
        except archive.ArchiveError as error:
            return self._archive_blocked(
                error,
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        return self._completed(revalidated, completed_backup, idempotent=False)

    def _resolve_attempt(self, evidence: _Evidence, backup: _Backup) -> CommandResult:
        if evidence.observed_remote_sha is not None:
            return self._blocked(
                "remote_delete_outcome_unknown",
                "A previous remote deletion attempt has no completion receipt.",
                leases=evidence.leases,
                actions=self._backup_actions(evidence, backup.artifact),
            )
        try:
            completed_backup = self._write_receipt(
                evidence, completion="remote_absent_on_retry"
            )
        except archive.ArchiveError as error:
            return self._archive_blocked(
                error,
                leases=evidence.leases,
                actions=self._backup_actions(evidence, backup.artifact),
            )
        return self._completed(evidence, completed_backup, idempotent=False)

    def _completion_preview(self, evidence: _Evidence, backup: _Backup) -> CommandResult:
        return CommandResult.ok(
            _COMMAND,
            decision="preview",
            actions=(
                *self._backup_actions(evidence, backup.artifact),
                {
                    "kind": "delete_remote_branch",
                    "branch": evidence.branch,
                    "remote_sha": evidence.expected_sha,
                    "idempotent": False,
                    "completion": "remote_absent_on_retry",
                },
            ),
        )

    def _create_or_verify_backup(self, evidence: _Evidence) -> _Backup:
        existing = self._read_existing_backup(evidence)
        if existing is not None:
            return existing
        self._prepare_namespace(evidence)
        try:
            evidence.destination.mkdir(mode=0o700)
        except FileExistsError:
            return self._read_existing_backup_required(evidence)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_write_failed", "unable to create remote discard backup directory"
            ) from error
        self._require_private_directory(evidence.destination)
        archive._fsync_directory(evidence.destination.parent)
        bundle = evidence.destination / "history.bundle"
        try:
            self.git.create_detached_commit_bundle(bundle, commit_sha=evidence.expected_sha)
            archive._seal_private_file(bundle)
            self.git.verify_bundle(bundle, expected_head=evidence.expected_sha)
            artifact = archive._artifact_record(bundle)
            archive._write_private_json(
                evidence.destination / "manifest.json", self._manifest(evidence, artifact)
            )
            archive._fsync_private_file(bundle)
            archive._fsync_private_file(evidence.destination / "manifest.json")
            archive._fsync_directory(evidence.destination)
            archive._fsync_directory(evidence.destination.parent)
        except archive.ArchiveError:
            raise
        except (GitError, OSError) as error:
            raise GitError("unable to create and verify detached commit bundle") from error
        return self._read_existing_backup_required(evidence)

    def _read_existing_backup_required(self, evidence: _Evidence) -> _Backup:
        backup = self._read_existing_backup(evidence)
        if backup is None:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard backup disappeared during verification"
            )
        return backup

    def _read_existing_backup(self, evidence: _Evidence) -> _Backup | None:
        try:
            destination_details = evidence.destination.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard backup cannot be inspected"
            ) from error
        if (
            stat.S_ISLNK(destination_details.st_mode)
            or not stat.S_ISDIR(destination_details.st_mode)
            or destination_details.st_uid != os.getuid()
            or stat.S_IMODE(destination_details.st_mode) != 0o700
        ):
            raise archive.ArchiveError(
                "archive_unsafe", "remote discard backup directory is not private"
            )
        self._validate_namespace(evidence)
        try:
            with os.scandir(evidence.destination) as scanner:
                names = {entry.name for entry in scanner}
        except OSError as error:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard backup directory cannot be inspected"
            ) from error
        required = {"history.bundle", "manifest.json"}
        allowed = {"history.bundle", "manifest.json", "attempt.json", "receipt.json"}
        if not required.issubset(names) or names - allowed:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard backup has an invalid artifact layout"
            )
        attempt = self._read_marker_optional(
            evidence.destination, "attempt.json", self._attempt_schema
        )
        receipt = self._read_marker_optional(
            evidence.destination, "receipt.json", self._receipt_schema
        )
        if receipt is not None and attempt is None:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard receipt has no deletion attempt"
            )
        expected_names = required | ({"attempt.json"} if attempt is not None else set()) | (
            {"receipt.json"} if receipt is not None else set()
        )
        if names != expected_names:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard backup has unexpected artifacts"
            )
        manifest = archive._read_manifest(
            evidence.destination / "manifest.json", maximum_bytes=_MAX_JSON_BYTES
        )
        artifact = self._validate_manifest(evidence, manifest)
        if attempt is not None:
            self._validate_attempt(evidence, attempt)
        if receipt is not None:
            self._validate_receipt(evidence, receipt)
        try:
            archive._verify_artifact_record(evidence.destination / "history.bundle", artifact)
            self.git.verify_bundle(
                evidence.destination / "history.bundle", expected_head=evidence.expected_sha
            )
        except archive.ArchiveError:
            raise
        except (GitError, OSError) as error:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard bundle cannot be independently verified"
            ) from error
        return _Backup(artifact=artifact, attempt=attempt, receipt=receipt)

    def _read_marker_optional(
        self, destination: Path, name: str, schema: str
    ) -> dict[str, Any] | None:
        path = destination / name
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard marker cannot be inspected"
            ) from error
        marker = archive._read_manifest(path, maximum_bytes=_MAX_JSON_BYTES)
        if marker.get("schema") != schema:
            raise archive.ArchiveError("archive_corrupt", "remote discard marker has invalid schema")
        return marker

    def _validate_manifest(
        self, evidence: _Evidence, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(manifest) != {"schema", "metadata", "artifacts", "restore"}:
            raise archive.ArchiveError("archive_corrupt", "remote discard manifest has invalid fields")
        if manifest.get("schema") != self._manifest_schema:
            raise archive.ArchiveError("archive_corrupt", "remote discard manifest has invalid schema")
        metadata = manifest.get("metadata")
        artifacts = manifest.get("artifacts")
        restore = manifest.get("restore")
        if not isinstance(metadata, Mapping) or not isinstance(artifacts, Mapping):
            raise archive.ArchiveError("archive_corrupt", "remote discard manifest is malformed")
        expected_metadata = {
            "repository_id": evidence.payload["repository_id"],
            "repository_root": evidence.payload["repository_root"],
            "branch": evidence.branch,
            self._sha_key: evidence.expected_sha,
            "reason": evidence.reason,
            "preview_token": evidence.token,
            "backup_kind": _BACKUP_KIND,
            "leases": evidence.payload["leases"],
        }
        if set(metadata) != {*expected_metadata, "created_at"} or any(
            metadata.get(key) != value for key, value in expected_metadata.items()
        ):
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard manifest provenance does not match"
            )
        if not isinstance(metadata.get("created_at"), str) or not metadata["created_at"]:
            raise archive.ArchiveError("archive_corrupt", "remote discard manifest timestamp is invalid")
        if set(artifacts) != {"history.bundle"}:
            raise archive.ArchiveError("archive_corrupt", "remote discard manifest artifacts are invalid")
        artifact = artifacts["history.bundle"]
        if not isinstance(artifact, dict):
            raise archive.ArchiveError("archive_corrupt", "remote discard bundle record is invalid")
        expected_restore = self._restore(evidence)
        if restore != expected_restore:
            raise archive.ArchiveError("archive_corrupt", "remote discard restore metadata is invalid")
        return artifact

    def _validate_attempt(self, evidence: _Evidence, attempt: Mapping[str, Any]) -> None:
        if set(attempt) != {
            "schema",
            "token",
            "repository_id",
            "branch",
            self._sha_key,
            "attempted_at",
        } or any(
            attempt.get(key) != value
            for key, value in {
                "schema": self._attempt_schema,
                "token": evidence.token,
                "repository_id": evidence.payload["repository_id"],
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
            }.items()
        ) or not isinstance(attempt.get("attempted_at"), str) or not attempt["attempted_at"]:
            raise archive.ArchiveError("archive_corrupt", "remote discard attempt is invalid")

    def _validate_receipt(self, evidence: _Evidence, receipt: Mapping[str, Any]) -> None:
        if set(receipt) != {
            "schema",
            "token",
            "repository_id",
            "branch",
            self._sha_key,
            "deleted_at",
            "completion",
        } or any(
            receipt.get(key) != value
            for key, value in {
                "schema": self._receipt_schema,
                "token": evidence.token,
                "repository_id": evidence.payload["repository_id"],
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
            }.items()
        ) or not isinstance(receipt.get("deleted_at"), str) or not receipt["deleted_at"] or receipt.get(
            "completion"
        ) not in self._valid_completions:
            raise archive.ArchiveError("archive_corrupt", "remote discard receipt is invalid")

    def _prepare_namespace(self, evidence: _Evidence) -> None:
        validated_root = archive.validate_backup_root(
            evidence.backup_root, forbidden_roots=()
        )
        if validated_root != evidence.backup_root:
            raise archive.ArchiveError(
                "backup_root_invalid", "backup root changed during remote discard"
            )
        parent = validated_root
        for component in (evidence.payload["repository_id"], self._namespace):
            candidate = parent / component
            try:
                details = candidate.lstat()
            except FileNotFoundError:
                try:
                    candidate.mkdir(mode=0o700)
                except FileExistsError:
                    details = candidate.lstat()
                except OSError as error:
                    raise archive.ArchiveError(
                        "archive_write_failed", "unable to create remote discard namespace"
                    ) from error
                else:
                    details = candidate.lstat()
                    archive._fsync_directory(parent)
            except OSError as error:
                raise archive.ArchiveError(
                    "archive_unsafe", "unable to inspect remote discard namespace"
                ) from error
            if not self._is_private_directory(details):
                raise archive.ArchiveError(
                    "archive_unsafe", "remote discard namespace is not private"
                )
            parent = candidate
        if parent != evidence.destination.parent:
            raise archive.ArchiveError(
                "archive_unsafe", "remote discard destination escapes its namespace"
            )
        validated_parent = archive.validate_backup_root(parent, forbidden_roots=())
        if validated_parent != parent:
            raise archive.ArchiveError(
                "archive_unsafe", "remote discard namespace changed during validation"
            )

    def _validate_namespace(self, evidence: _Evidence) -> None:
        parent = evidence.backup_root
        for component in (evidence.payload["repository_id"], self._namespace):
            parent /= component
            try:
                details = parent.lstat()
            except OSError as error:
                raise archive.ArchiveError(
                    "archive_unsafe", "remote discard namespace is unavailable"
                ) from error
            if not self._is_private_directory(details):
                raise archive.ArchiveError(
                    "archive_unsafe", "remote discard namespace is not private"
                )
        if parent != evidence.destination.parent:
            raise archive.ArchiveError(
                "archive_unsafe", "remote discard destination escapes its namespace"
            )

    def _clear_failed_attempt(self, evidence: _Evidence) -> None:
        """Retire an attempt only when Git confirmed the push never deleted a ref."""
        marker = evidence.destination / "attempt.json"
        archive._validate_private_file(marker)
        marker.unlink()
        archive._fsync_directory(evidence.destination)

    def _write_attempt(self, evidence: _Evidence) -> None:
        self._write_marker(
            evidence,
            "attempt.json",
            {
                "schema": self._attempt_schema,
                "token": evidence.token,
                "repository_id": evidence.payload["repository_id"],
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
                "attempted_at": now_iso(),
            },
        )

    def _write_receipt(self, evidence: _Evidence, *, completion: str) -> _Backup:
        if completion not in self._valid_completions:
            raise ValueError(f"invalid {self._discard_subject} completion")
        self._write_marker(
            evidence,
            "receipt.json",
            {
                "schema": self._receipt_schema,
                "token": evidence.token,
                "repository_id": evidence.payload["repository_id"],
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
                "deleted_at": now_iso(),
                "completion": completion,
            },
        )
        return self._read_existing_backup_required(evidence)

    def _write_marker(
        self, evidence: _Evidence, name: str, marker: dict[str, Any]
    ) -> None:
        path = evidence.destination / name
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard marker cannot be inspected"
            ) from error
        else:
            raise archive.ArchiveError(
                "archive_corrupt", "remote discard marker already exists"
            )
        archive._write_private_json(path, marker)
        archive._fsync_private_file(path)
        archive._fsync_directory(evidence.destination)
        archive._fsync_directory(evidence.destination.parent)

    def _manifest(self, evidence: _Evidence, artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": self._manifest_schema,
            "metadata": {
                "repository_id": evidence.payload["repository_id"],
                "repository_root": evidence.payload["repository_root"],
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
                "reason": evidence.reason,
                "preview_token": evidence.token,
                "backup_kind": _BACKUP_KIND,
                "leases": evidence.payload["leases"],
                "created_at": now_iso(),
            },
            "artifacts": {"history.bundle": artifact},
            "restore": self._restore(evidence),
        }

    def _restore(self, evidence: _Evidence) -> dict[str, Any]:
        return {
            "head_sha": evidence.expected_sha,
            "commands": [
                'git clone --bare "$PWD/history.bundle" "${PWD}-restored.git"',
                'git --git-dir="${PWD}-restored.git" fsck --full --strict',
            ],
            "notes": (
                f"The restoration contains only the commit graph reachable from the {self._bundle_source} "
                "HEAD (commits, trees, and blobs). It does not include worktree files, uncommitted "
                "changes, the index, reflogs, local refs, tags, or PR metadata. The restored bare "
                "repository is a sibling of this archive."
            ),
        }

    @staticmethod
    def _token_payload(
        *,
        repository_id: str,
        repository_root: Path,
        origin_url_sha256: str,
        branch: str,
        expected_sha: str,
        backup_root: Path,
        reason: str,
        leases: tuple[Lease, ...],
    ) -> dict[str, Any]:
        return {
            "schema": _TOKEN_SCHEMA,
            "repository_id": repository_id,
            "repository_root": str(repository_root),
            "origin_url_sha256": origin_url_sha256,
            "branch": branch,
            "expected_sha": expected_sha,
            "backup_root": str(backup_root),
            "backup_kind": _BACKUP_KIND,
            "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            "leases": [
                {
                    "id": lease.id,
                    "version": lease.version,
                    "state": lease.state.value,
                    "head_sha": lease.head_sha,
                    "retain": lease.retain,
                }
                for lease in leases
            ],
        }

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
    def _strip_refs(value: str | None) -> str:
        if not value:
            return ""
        for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
            if value.startswith(prefix):
                return value[len(prefix) :]
        return value

    def _preview_actions(self, evidence: _Evidence) -> tuple[dict[str, Any], ...]:
        return (
            {
                "kind": "create_commit_bundle",
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
                "backup_directory": str(evidence.destination),
                "preview_token": evidence.token,
            },
            {
                "kind": self._delete_action_kind,
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
            },
        )

    def _backup_actions(
        self, evidence: _Evidence, artifact: Mapping[str, Any]
    ) -> tuple[dict[str, Any], ...]:
        bundle_bytes = artifact.get("bytes")
        if not isinstance(bundle_bytes, int) or isinstance(bundle_bytes, bool):
            raise archive.ArchiveError("archive_corrupt", "remote discard bundle size is invalid")
        return (
            {
                "kind": "create_commit_bundle",
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
                "backup_directory": str(evidence.destination),
                "preview_token": evidence.token,
                "bundle_bytes": bundle_bytes,
            },
        )

    def _completed(
        self, evidence: _Evidence, backup: _Backup, *, idempotent: bool
    ) -> CommandResult:
        assert backup.receipt is not None
        actions = (
            *self._backup_actions(evidence, backup.artifact),
            {
                "kind": self._delete_action_kind,
                "branch": evidence.branch,
                self._sha_key: evidence.expected_sha,
                "idempotent": idempotent,
                "completion": backup.receipt["completion"],
            },
        )
        warnings: tuple[dict[str, str], ...] = ()
        if idempotent and evidence.observed_remote_sha is not None:
            warnings = (
                {
                    "code": "branch_recreated_untouched",
                    "message": f"The {self._branch_subject} exists again and was left untouched.",
                },
            )
        return CommandResult.ok(
            self._command,
            decision="discarded",
            actions=actions,
            warnings=warnings,
        )

    @staticmethod
    def _is_private_directory(details: os.stat_result) -> bool:
        return (
            not stat.S_ISLNK(details.st_mode)
            and stat.S_ISDIR(details.st_mode)
            and details.st_uid == os.getuid()
            and stat.S_IMODE(details.st_mode) == 0o700
        )

    def _require_private_directory(self, path: Path) -> None:
        try:
            details = path.lstat()
        except OSError as error:
            raise archive.ArchiveError(
                "archive_write_failed", f"{self._discard_subject} directory is unavailable"
            ) from error
        if not self._is_private_directory(details):
            raise archive.ArchiveError(
                "archive_unsafe", f"{self._discard_subject} directory is not private"
            )

    @classmethod
    def _archive_corrupt(cls, evidence: _Evidence) -> CommandResult:
        return cls._blocked(
            "archive_corrupt",
            f"The {cls._discard_subject} backup cannot be independently verified.",
            leases=evidence.leases,
        )

    @classmethod
    def _archive_blocked(
        cls,
        error: archive.ArchiveError,
        *,
        leases: tuple[Lease, ...] = (),
        actions: tuple[dict[str, Any], ...] = (),
    ) -> CommandResult:
        return cls._blocked(
            error.code,
            f"{cls._discard_subject.capitalize()} backup validation failed.",
            leases=leases,
            actions=actions,
        )

    @classmethod
    def _external_error(
        cls,
        code: str,
        message: str,
        *,
        leases: tuple[Lease, ...] = (),
        actions: tuple[dict[str, Any], ...] = (),
    ) -> CommandResult:
        return CommandResult.external_error(
            cls._command, code=code, message=message, actions=actions
        )

    @classmethod
    def _blocked(
        cls,
        code: str,
        message: str,
        *,
        leases: tuple[Lease, ...] = (),
        actions: tuple[dict[str, Any], ...] = (),
    ) -> CommandResult:
        return CommandResult.blocked(
            cls._command,
            blockers=({"code": code, "message": message},),
            actions=actions,
        )
