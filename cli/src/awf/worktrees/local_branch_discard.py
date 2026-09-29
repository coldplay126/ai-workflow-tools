from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import archive
from .git import GitBranchDeleteAborted, GitError
from .github import ExternalServiceError, GhClient
from .models import CommandResult, Lease
from .remote_branch_discard import _BACKUP_KIND, _Backup, _Evidence, RemoteBranchDiscarder


_COMMAND = "wt.discard-local-branch"
_TOKEN_SCHEMA = "awf.discard-local-branch/v1"
_MANIFEST_SCHEMA = "awf.discard-local-branch-manifest/v1"
_ATTEMPT_SCHEMA = "awf.discard-local-branch-attempt/v1"
_RECEIPT_SCHEMA = "awf.discard-local-branch-receipt/v1"
_NAMESPACE = "local-branch-discard"


class LocalBranchDiscarder(RemoteBranchDiscarder):
    """Back up an approved local ref before deleting it with a local CAS."""

    _command = _COMMAND
    _token_schema = _TOKEN_SCHEMA
    _manifest_schema = _MANIFEST_SCHEMA
    _attempt_schema = _ATTEMPT_SCHEMA
    _receipt_schema = _RECEIPT_SCHEMA
    _namespace = _NAMESPACE
    _sha_key = "local_sha"
    _delete_action_kind = "delete_local_branch"
    _branch_subject = "local branch"
    _discard_subject = "local discard"
    _bundle_source = "local branch"
    _absent_completion = "local_absent_on_retry"
    _valid_completions = frozenset({"deleted", "local_absent_on_retry"})
    _operation_label = "local discard"

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
            repository_root = self.git.repository_root()
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
        try:
            repository_leases = self._repository_leases(leases, repository_id)
        except (GitError, OSError):
            return self._blocked(
                "repository_inspection_failed", "Unable to inspect the Git common directory."
            )
        try:
            default_remote_branch = self.git.default_remote_branch()
        except (GitError, OSError):
            return self._blocked(
                "default_branch_unknown", "Unable to determine origin's default branch."
            )
        protected = self._protected_branch_blocker(
            branch,
            default_remote_branch=default_remote_branch,
            leases=repository_leases,
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
                (lease for lease in repository_leases if lease.branch == branch),
                key=lambda lease: lease.id,
            )
        )
        lease_blocker = self._lease_blocker(matching_leases)
        if lease_blocker is not None:
            return lease_blocker
        try:
            local_commit = self.git.resolve_ref(f"{expected_sha}^{{commit}}")
        except (GitError, OSError):
            return self._blocked(
                "local_commit_missing",
                "The approved local commit is unavailable as a local commit object.",
                leases=matching_leases,
            )
        if local_commit != expected_sha:
            return self._blocked(
                "local_commit_missing",
                "The approved local commit is unavailable as a local commit object.",
                leases=matching_leases,
            )
        try:
            remote_repository = self.git.remote_url()
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
                "Unable to inspect repository metadata for the branch.",
                leases=matching_leases,
            )
        if open_pull_requests:
            return self._blocked(
                "open_pull_request",
                "The branch has an open pull request and cannot be discarded.",
                leases=matching_leases,
            )
        try:
            observed_local_sha = self.git.local_branch_sha(branch)
        except (GitError, OSError):
            return self._blocked(
                "local_branch_inspection_failed",
                "Unable to inspect the local branch.",
                leases=matching_leases,
            )
        payload = self._local_token_payload(
            repository_id=repository_id,
            repository_root=repository_root,
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
            observed_remote_sha=observed_local_sha,
            reason=reason,
            backup_root=validated_backup_root,
            destination=(
                validated_backup_root
                / repository_id
                / self._namespace
                / self._stable_intent_digest(payload)
            ),
            leases=matching_leases,
        )

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
                    "local_delete_outcome_unknown",
                    "A previous local deletion attempt has no completion receipt.",
                    leases=evidence.leases,
                    actions=self._backup_actions(evidence, backup.artifact),
                )
            return self._completion_preview(evidence, backup)
        if evidence.observed_remote_sha is None:
            return self._blocked(
                "local_branch_absent",
                "The local branch is absent without a prior deletion attempt.",
                leases=evidence.leases,
            )
        if evidence.observed_remote_sha != evidence.expected_sha:
            return self._blocked(
                "local_head_mismatch",
                "The local branch no longer matches the approved commit.",
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
                "local_branch_absent",
                "The local branch is absent without a prior deletion attempt.",
                leases=evidence.leases,
            )
        if evidence.observed_remote_sha != evidence.expected_sha:
            return self._blocked(
                "local_head_changed",
                "The local branch no longer matches the approved commit.",
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
                "local_head_changed",
                "The local branch changed before deletion.",
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
            self.git.delete_inactive_branch_if_at(
                revalidated.branch,
                revalidated.expected_sha,
                before_commit=lambda: self._write_attempt(revalidated),
            )
        except GitBranchDeleteAborted:
            try:
                try:
                    (revalidated.destination / "attempt.json").lstat()
                except FileNotFoundError:
                    pass
                else:
                    self._clear_failed_attempt(revalidated)
            except (archive.ArchiveError, OSError):
                return self._blocked(
                    "local_delete_outcome_unknown",
                    "The aborted transaction could not be recorded safely.",
                    leases=revalidated.leases,
                    actions=self._backup_actions(revalidated, revalidated_backup.artifact),
                )
            return self._blocked(
                "local_delete_failed",
                "The local branch deletion was aborted before commit; retry this token.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        except archive.ArchiveError as error:
            return self._archive_blocked(
                error,
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        except GitError:
            return self._blocked(
                "local_delete_failed",
                "The local branch deletion could not be confirmed.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        try:
            post_delete_local_sha = self.git.local_branch_sha(revalidated.branch)
        except (GitError, OSError):
            return self._blocked(
                "local_delete_outcome_unknown",
                "The local deletion completed but could not be independently verified.",
                leases=revalidated.leases,
                actions=self._backup_actions(revalidated, revalidated_backup.artifact),
            )
        if post_delete_local_sha is not None:
            return self._blocked(
                "local_delete_outcome_unknown",
                "The local branch still exists after the deletion request.",
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
                "local_delete_outcome_unknown",
                "A previous local deletion attempt has no completion receipt.",
                leases=evidence.leases,
                actions=self._backup_actions(evidence, backup.artifact),
            )
        try:
            completed_backup = self._write_receipt(
                evidence, completion=self._absent_completion
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
            self._command,
            decision="preview",
            actions=(
                *self._backup_actions(evidence, backup.artifact),
                {
                    "kind": self._delete_action_kind,
                    "branch": evidence.branch,
                    self._sha_key: evidence.expected_sha,
                    "idempotent": False,
                    "completion": self._absent_completion,
                },
            ),
        )

    @classmethod
    def _local_token_payload(
        cls,
        *,
        repository_id: str,
        repository_root: Path,
        branch: str,
        expected_sha: str,
        backup_root: Path,
        reason: str,
        leases: tuple[Lease, ...],
    ) -> dict[str, object]:
        return {
            "schema": cls._token_schema,
            "repository_id": repository_id,
            "repository_root": str(repository_root),
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

    @classmethod
    def _stable_intent_digest(cls, payload: Mapping[str, object]) -> str:
        return cls._token(
            {key: value for key, value in payload.items() if key != "leases"}
        )

    def _validate_manifest(
        self, evidence: _Evidence, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        recorded_evidence = self._recorded_manifest_evidence(evidence, manifest)
        if recorded_evidence is None:
            return super()._validate_manifest(evidence, manifest)
        artifact = super()._validate_manifest(recorded_evidence, manifest)
        if not self._tokens_match(evidence.token, recorded_evidence.token):
            raise archive.ArchiveError(
                "local_intent_evidence_changed",
                "A previous local discard backup has different immutable evidence.",
            )
        return artifact

    def _recorded_manifest_evidence(
        self, evidence: _Evidence, manifest: Mapping[str, Any]
    ) -> _Evidence | None:
        metadata = manifest.get("metadata")
        if (
            manifest.get("schema") != self._manifest_schema
            or not isinstance(metadata, Mapping)
            or set(metadata)
            != {
                "repository_id",
                "repository_root",
                "branch",
                self._sha_key,
                "reason",
                "preview_token",
                "backup_kind",
                "leases",
                "created_at",
            }
            or not all(
                isinstance(metadata[key], str)
                for key in (
                    "repository_id",
                    "repository_root",
                    "branch",
                    self._sha_key,
                    "reason",
                    "preview_token",
                    "backup_kind",
                    "created_at",
                )
            )
            or not isinstance(metadata["leases"], list)
        ):
            return None
        recorded_payload: dict[str, object] = {
            "schema": self._token_schema,
            "repository_id": metadata["repository_id"],
            "repository_root": metadata["repository_root"],
            "branch": metadata["branch"],
            "expected_sha": metadata[self._sha_key],
            "backup_root": str(evidence.backup_root),
            "backup_kind": metadata["backup_kind"],
            "reason_sha256": hashlib.sha256(
                metadata["reason"].encode("utf-8")
            ).hexdigest(),
            "leases": metadata["leases"],
        }
        recorded_token = metadata["preview_token"]
        if (
            self._stable_intent_digest(recorded_payload)
            != self._stable_intent_digest(evidence.payload)
            or not self._tokens_match(recorded_token, self._token(recorded_payload))
        ):
            return None
        return replace(evidence, token=recorded_token, payload=recorded_payload)
