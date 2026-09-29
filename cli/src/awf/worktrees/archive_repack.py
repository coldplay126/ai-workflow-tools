from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
from typing import Any

from . import archive
from .git import GitClient, GitError
from .locking import repository_lock
from .models import CommandResult, Lease, LeaseState
from .registry import WorktreeRegistry


_COMMAND = "wt.archive-repack"
_JOURNAL_SCHEMA = "awf.archive-repack/v1"
_TOKEN_SCHEMA = "awf.archive-repack-token/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700
_MAX_JOURNAL_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _ArchiveRecord:
    identity: dict[str, int]
    manifest_sha256: str
    artifacts: dict[str, dict[str, Any]]
    snapshot_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "manifest_sha256": self.manifest_sha256,
            "artifacts": self.artifacts,
            "snapshot_fingerprint": self.snapshot_fingerprint,
        }


@dataclass(frozen=True)
class _Evidence:
    source: Path
    private_root: Path
    lease: Lease
    policy: tuple[str, ...]
    token: str
    payload: dict[str, Any]
    source_record: _ArchiveRecord
    excluded_entries: int | None
    excluded_bytes: int | None

class ArchiveRepacker:
    """Replace one removed-worktree archive without exposing an unverified backup.

    The transaction has a durable receipt next to the archive.  A receipt survives a
    completed operation so a repeated apply token is unambiguously idempotent.  It
    never contains archived file contents or manifest metadata.
    """

    def __init__(
        self,
        registry: WorktreeRegistry,
        git: GitClient,
        cache_dir: Path,
        lock_dir: Path,
    ) -> None:
        self.registry = registry
        self.git = git
        self.cache_dir = Path(cache_dir).resolve()
        self.lock_dir = Path(lock_dir).resolve()

    def run(
        self,
        archive_path: Path,
        *,
        exclude_ignored_paths: tuple[str, ...] = (),
        preview_token: str | None = None,
        apply: bool = False,
    ) -> CommandResult:
        arguments = self._validate_arguments(
            archive_path,
            exclude_ignored_paths=exclude_ignored_paths,
            preview_token=preview_token,
            apply=apply,
        )
        if isinstance(arguments, CommandResult):
            return arguments
        source, policy = arguments

        try:
            if not apply:
                return self._preview(source, policy)
            assert preview_token is not None
            with repository_lock(self._lock_path(source)):
                return self._apply(source, policy, preview_token)
        except archive.ArchiveError as error:
            return self._blocked(error.code, "Archive backup validation failed.")
        except (GitError, sqlite3.Error, OSError, RuntimeError, ValueError):
            return self._blocked(
                "archive_repack_failed",
                "The archive repack transaction could not be completed safely.",
            )

    def _validate_arguments(
        self,
        archive_path: Path,
        *,
        exclude_ignored_paths: tuple[str, ...],
        preview_token: str | None,
        apply: bool,
    ) -> tuple[Path, tuple[str, ...]] | CommandResult:
        if not isinstance(apply, bool):
            return self._blocked("invalid_apply", "apply must be a boolean")
        if not isinstance(archive_path, Path) or not archive_path.is_absolute():
            return self._blocked("invalid_archive", "archive must be an absolute path")
        if ".." in archive_path.parts or _SHA256.fullmatch(archive_path.name) is None:
            return self._blocked("invalid_archive", "archive path is not a valid archive identity")
        if not isinstance(exclude_ignored_paths, tuple):
            return self._blocked(
                "invalid_exclude_ignored_path",
                "exclude_ignored_paths must be a tuple of relative paths",
            )
        try:
            policy = self._normalize_policy(exclude_ignored_paths)
        except ValueError:
            return self._blocked(
                "invalid_exclude_ignored_path",
                "exclude_ignored_paths must contain normalized relative paths",
            )
        if apply and (
            not isinstance(preview_token, str)
            or _SHA256.fullmatch(preview_token) is None
        ):
            return self._blocked(
                "preview_token_required",
                "apply requires the exact preview token returned by preview",
            )
        return archive_path, policy

    def _preview(self, supplied: Path, policy: tuple[str, ...]) -> CommandResult:
        source = self._canonical_parent_path(supplied)
        journal = self._read_journal_optional(source)
        if journal is not None:
            evidence = self._evidence_from_journal(source, journal)
            if evidence.policy != policy:
                return self._blocked(
                    "repack_policy_mismatch",
                    "The existing repack transaction uses a different exclusion policy.",
                    lease=evidence.lease,
                )
            if journal["state"] == "completed":
                self._verify_completed(source, evidence, journal, cleanup=False)
                return self._repacked(evidence, idempotent=True)
            return self._blocked(
                "repack_recovery_required",
                "A prior archive repack transaction must be resumed with its preview token.",
                lease=evidence.lease,
            )

        evidence = self._inspect_source(source, policy)
        return self._preview_result(evidence)

    def _apply(
        self, supplied: Path, policy: tuple[str, ...], preview_token: str
    ) -> CommandResult:
        source = self._canonical_parent_path(supplied)
        journal = self._read_journal_optional(source)
        if journal is not None:
            evidence = self._evidence_from_journal(source, journal)
            if evidence.policy != policy:
                return self._blocked(
                    "repack_policy_mismatch",
                    "The supplied exclusions do not match the existing repack transaction.",
                    lease=evidence.lease,
                )
            if not hmac.compare_digest(preview_token, evidence.token):
                return self._blocked(
                    "preview_token_mismatch",
                    "The preview token no longer matches this archive repack transaction.",
                    lease=evidence.lease,
                )
            if journal["state"] == "completed":
                self._verify_completed(source, evidence, journal, cleanup=True)
                return self._repacked(evidence, idempotent=True)
            return self._resume(source, evidence, journal)

        evidence = self._inspect_source(
            source, policy, validate_exclusion_policy=False
        )
        if not hmac.compare_digest(preview_token, evidence.token):
            return self._blocked(
                "preview_token_mismatch",
                "The preview token no longer matches the source archive.",
                lease=evidence.lease,
            )
        return self._start(source, evidence)

    def _inspect_source(
        self,
        supplied: Path,
        policy: tuple[str, ...],
        *,
        validate_exclusion_policy: bool = True,
    ) -> _Evidence:
        source = self._canonical_existing_archive(supplied)
        manifest = archive.read_verified_archive(source)
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping):
            raise archive.ArchiveError(
                "archive_provenance_mismatch", "archive lacks private provenance metadata"
            )
        lease_values = metadata.get("lease")
        if not isinstance(lease_values, Mapping):
            raise archive.ArchiveError(
                "archive_provenance_mismatch", "archive lacks its removal lease identity"
            )
        lease_id = lease_values.get("id")
        repository_id = metadata.get("repository_id")
        archive_token = metadata.get("preview_token")
        if (
            not isinstance(lease_id, str)
            or not lease_id
            or not isinstance(repository_id, str)
            or _SHA256.fullmatch(repository_id) is None
            or not isinstance(archive_token, str)
            or _SHA256.fullmatch(archive_token) is None
            or archive_token != source.name
        ):
            raise archive.ArchiveError(
                "archive_provenance_mismatch", "archive provenance is not a private archive identity"
            )
        lease = self._validate_current_lease(lease_id, repository_id, lease_values, metadata)
        private_root = self._validate_private_namespace(source, lease, archive_token)
        excluded_entries: int | None = None
        excluded_bytes: int | None = None
        if validate_exclusion_policy:
            filtered_snapshot = archive.prepare_repack_snapshot(
                archive_path=source,
                source_manifest=manifest,
                exclude_ignored_paths=policy,
            )
            excluded_entries, excluded_bytes = self._exclusion_summary(
                manifest, filtered_snapshot
            )
        record = self._record_archive(source, manifest)
        payload = self._token_payload(source, lease, record, policy)
        return _Evidence(
            source=source,
            private_root=private_root,
            lease=lease,
            policy=policy,
            token=self._token(payload),
            payload=payload,
            source_record=record,
            excluded_entries=excluded_entries,
            excluded_bytes=excluded_bytes,
        )

    def _validate_current_lease(
        self,
        lease_id: str,
        repository_id: str,
        recorded_lease: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> Lease:
        try:
            lease = self.registry.get_lease_read_only(lease_id)
            reservation = self.registry.get_cleanup_reservation(lease_id)
            repository_root = self.git.repository_root().resolve()
            current_repository_id = self.git.repository_id()
            repository_name = self.git.repository_name()
        except sqlite3.Error as error:
            raise archive.ArchiveError(
                "registry_conflict", "unable to read the worktree registry"
            ) from error
        except (GitError, OSError, RuntimeError) as error:
            raise archive.ArchiveError(
                "repository_inspection_failed", "unable to inspect the current repository"
            ) from error
        if lease is None:
            raise archive.ArchiveError("unknown_lease", "archive lease no longer exists")
        if lease.state is not LeaseState.REMOVED:
            raise archive.ArchiveError(
                "archive_not_removed", "archive repack requires a removed worktree lease"
            )
        if reservation is not None:
            raise archive.ArchiveError(
                "cleanup_reserved", "archive repack cannot run while cleanup is reserved"
            )
        try:
            lease_root = lease.repository_root.resolve()
        except (OSError, RuntimeError) as error:
            raise archive.ArchiveError(
                "repository_mismatch", "lease repository root cannot be resolved"
            ) from error
        required = {
            "id": lease.id,
            "repository_id": lease.repository_id,
            "repository_name": lease.repository_name,
            "repository_root": str(lease.repository_root),
            "worktree_path": str(lease.worktree_path),
            "branch": lease.branch,
        }
        if any(recorded_lease.get(name) != value for name, value in required.items()):
            raise archive.ArchiveError(
                "archive_provenance_mismatch", "archive lease identity does not match the registry"
            )
        head_sha = metadata.get("head_sha")
        discard_evidence = metadata.get("archive_discard_evidence")
        if (
            not isinstance(head_sha, str)
            or _OBJECT_ID.fullmatch(head_sha) is None
            or metadata.get("repository_id") != lease.repository_id
            or not isinstance(discard_evidence, Mapping)
            or discard_evidence.get("head_sha") != head_sha
            or discard_evidence.get("lease_id") != lease.id
            or discard_evidence.get("repository_id") != lease.repository_id
        ):
            raise archive.ArchiveError(
                "archive_provenance_mismatch", "archive repository evidence does not match its lease"
            )
        if (
            repository_id != lease.repository_id
            or current_repository_id != lease.repository_id
            or repository_name != lease.repository_name
            or lease_root != repository_root
        ):
            raise archive.ArchiveError(
                "repository_mismatch", "archive does not belong to the current repository"
            )
        return lease

    def _validate_private_namespace(
        self, source: Path, lease: Lease, token: str, *, source_may_be_absent: bool = False
    ) -> Path:
        private_root = source.parent.parent.parent
        if source.relative_to(private_root).parts != (
            lease.repository_id,
            lease.id,
            token,
        ):
            raise archive.ArchiveError(
                "archive_identity_mismatch", "archive path does not match its lease identity"
            )
        try:
            all_leases = self.registry.list_leases_read_only(include_removed=True)
        except sqlite3.Error as error:
            raise archive.ArchiveError(
                "registry_conflict", "unable to identify private archive boundaries"
            ) from error
        forbidden = (self.git.repository_root(), self.cache_dir) + tuple(
            item.worktree_path for item in all_leases
        )
        validated_root = archive.validate_backup_root(
            private_root, forbidden_roots=forbidden
        )
        if validated_root != private_root:
            raise archive.ArchiveError(
                "archive_identity_mismatch", "archive private root changed during validation"
            )
        for directory in (source.parent.parent, source.parent):
            self._require_private_directory(directory)
        if not source_may_be_absent or self._exists(source):
            self._require_private_directory(source)
        return private_root

    def _token_payload(
        self,
        source: Path,
        lease: Lease,
        record: _ArchiveRecord,
        policy: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "schema": _TOKEN_SCHEMA,
            "archive_path": str(source),
            "repository_id": lease.repository_id,
            "lease_id": lease.id,
            "source": record.to_dict(),
            "exclude_ignored_paths": list(policy),
        }

    def _start(self, source: Path, evidence: _Evidence) -> CommandResult:
        staging = self._new_sibling(source, "staging")
        previous = self._new_sibling(source, "previous")
        try:
            derivative = archive.repack_archive_contents(
                source=source,
                destination=staging,
                exclude_ignored_paths=evidence.policy,
            )
            candidate = self._candidate_record(staging, derivative, evidence)
        except archive.ArchiveError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to create the replacement archive"
            ) from error

        journal = self._new_journal(evidence, staging, previous)
        self._write_journal(self._journal_path(source), journal)
        return self._continue_transaction(source, evidence, journal, candidate=candidate)

    def _resume(
        self, source: Path, evidence: _Evidence, journal: dict[str, Any]
    ) -> CommandResult:
        return self._continue_transaction(source, evidence, journal, candidate=None)

    def _continue_transaction(
        self,
        source: Path,
        evidence: _Evidence,
        journal: dict[str, Any],
        *,
        candidate: _ArchiveRecord | None,
    ) -> CommandResult:
        staging = self._journal_sibling(source, journal["staging"], "staging")
        previous = self._journal_sibling(source, journal["previous"], "previous")
        source_exists = self._exists(source)
        previous_exists = self._exists(previous)

        if source_exists:
            if not previous_exists:
                if journal["state"] != "prepared":
                    self._verify_archive_record(source, evidence.source_record)
                    return self._finish_stale_transaction(source, evidence)
                return self._publish(source, evidence, journal, staging, previous, candidate)
            try:
                published, manifest = self._candidate_record_and_manifest_from_path(
                    source, evidence
                )
            except archive.ArchiveError:
                return self._restore_original(source, evidence, previous)
            return self._finalize(
                source,
                evidence,
                journal,
                previous,
                published,
                verified=(published, manifest),
            )

        if previous_exists:
            return self._publish(source, evidence, journal, staging, previous, candidate)
        raise archive.ArchiveError(
            "repack_recovery_required", "neither the source nor original archive is present"
        )

    def _publish(
        self,
        source: Path,
        evidence: _Evidence,
        journal: dict[str, Any],
        staging: Path,
        previous: Path,
        candidate: _ArchiveRecord | None,
    ) -> CommandResult:
        if self._exists(source):
            if self._exists(previous):
                raise archive.ArchiveError(
                    "repack_recovery_required", "previous archive already exists before publish"
                )
            try:
                candidate = self._verify_candidate(staging, evidence, candidate)
            except archive.ArchiveError:
                self._verify_archive_record(source, evidence.source_record)
                return self._finish_stale_transaction(source, evidence)
            self._verify_archive_record(source, evidence.source_record)
            self._rename(source, previous)
            self._fsync_directory(source.parent)
            journal = {**journal, "state": "source_moved"}
            self._write_journal(self._journal_path(source), journal)
        else:
            self._verify_archive_record(previous, evidence.source_record)

        try:
            candidate = self._verify_candidate(staging, evidence, candidate)
        except archive.ArchiveError:
            return self._restore_original(source, evidence, previous)
        self._rename(staging, source)
        self._fsync_directory(source.parent)
        journal = {**journal, "state": "published", "published": candidate.to_dict()}
        self._write_journal(self._journal_path(source), journal)
        return self._finalize(source, evidence, journal, previous, candidate)

    def _finalize(
        self,
        source: Path,
        evidence: _Evidence,
        journal: dict[str, Any],
        previous: Path,
        published: _ArchiveRecord,
        *,
        verified: tuple[_ArchiveRecord, dict[str, Any]] | None = None,
    ) -> CommandResult:
        actual, manifest = (
            verified
            if verified is not None
            else self._candidate_record_and_manifest_from_path(source, evidence)
        )
        if actual != published:
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "archive changed during the repack transaction"
            )
        self._fsync_archive(source, manifest)
        completed = {
            **journal,
            "state": "completed",
            "published": published.to_dict(),
            "previous": previous.name if self._exists(previous) else None,
        }
        self._write_journal(self._journal_path(source), completed)

        if self._exists(previous):
            self._delete_verified_archive(previous, evidence.source_record)
            self._fsync_directory(source.parent)
            completed = {**completed, "previous": None}
            self._write_journal(self._journal_path(source), completed)
        return self._repacked(evidence, idempotent=False)

    def _restore_original(
        self,
        source: Path,
        evidence: _Evidence,
        previous: Path,
    ) -> CommandResult:
        self._verify_archive_record(previous, evidence.source_record)
        if self._exists(source):
            self._require_private_directory(source)
            failed = self._new_sibling(source, "failed")
            self._rename(source, failed)
            self._fsync_directory(source.parent)
        self._rename(previous, source)
        self._fsync_directory(source.parent)
        self._remove_journal(self._journal_path(source))
        return self._blocked(
            "published_archive_invalid",
            "The replacement archive was invalid; the original archive was restored.",
            lease=evidence.lease,
        )

    def _finish_stale_transaction(
        self, source: Path, evidence: _Evidence
    ) -> CommandResult:
        self._remove_journal(self._journal_path(source))
        return self._blocked(
            "published_archive_invalid",
            "The replacement archive was invalid; the original archive was retained.",
            lease=evidence.lease,
        )

    def _verify_completed(
        self,
        source: Path,
        evidence: _Evidence,
        journal: Mapping[str, Any],
        *,
        cleanup: bool,
    ) -> None:
        published_value = journal.get("published")
        published = self._record_from_value(published_value)
        actual, _ = self._candidate_record_and_manifest_from_path(source, evidence)
        if actual != published:
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "archive changed during the repack transaction"
            )
        previous_name = journal.get("previous")
        if previous_name is None:
            return
        previous = self._journal_sibling(source, previous_name, "previous")
        if not self._exists(previous):
            if cleanup:
                self._write_journal(
                    self._journal_path(source), {**journal, "previous": None}
                )
            return
        self._validate_original_quarantine(previous, evidence.source_record)
        if not cleanup:
            return
        self._delete_verified_archive(previous, evidence.source_record)
        self._fsync_directory(source.parent)
        self._write_journal(
            self._journal_path(source), {**journal, "previous": None}
        )

    def _candidate_record(
        self,
        destination: Path,
        manifest: Mapping[str, Any],
        evidence: _Evidence,
    ) -> _ArchiveRecord:
        self._verify_derivative_manifest(manifest, evidence)
        return self._record_archive(destination, manifest)

    def _candidate_record_from_path(
        self, destination: Path, evidence: _Evidence
    ) -> _ArchiveRecord:
        record, _ = self._candidate_record_and_manifest_from_path(destination, evidence)
        return record

    def _candidate_record_and_manifest_from_path(
        self, destination: Path, evidence: _Evidence
    ) -> tuple[_ArchiveRecord, dict[str, Any]]:
        manifest = archive.read_verified_archive(destination)
        return self._candidate_record(destination, manifest, evidence), manifest

    def _verify_candidate(
        self,
        destination: Path,
        evidence: _Evidence,
        expected: _ArchiveRecord | None,
    ) -> _ArchiveRecord:
        actual = self._candidate_record_from_path(destination, evidence)
        if expected is not None and actual != expected:
            raise archive.ArchiveError(
                "archive_repack_failed", "replacement archive changed before publication"
            )
        return actual

    def _verify_derivative_manifest(
        self, manifest: Mapping[str, Any], evidence: _Evidence
    ) -> None:
        derivative = manifest.get("derivative")
        if not isinstance(derivative, Mapping):
            raise archive.ArchiveError(
                "archive_repack_failed", "replacement archive lacks derivative provenance"
            )
        if (
            derivative.get("source_manifest_sha256")
            != evidence.source_record.manifest_sha256
            or derivative.get("source_snapshot_fingerprint")
            != evidence.source_record.snapshot_fingerprint
            or derivative.get("excluded_paths") != list(evidence.policy)
        ):
            raise archive.ArchiveError(
                "archive_repack_failed", "replacement archive provenance does not match preview"
            )

    def _record_verified_archive(self, path: Path) -> _ArchiveRecord:
        manifest = archive.read_verified_archive(path)
        return self._record_archive(path, manifest)

    def _verify_archive_record(self, path: Path, expected: _ArchiveRecord) -> None:
        actual = self._record_verified_archive(path)
        if actual != expected:
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "archive changed during the repack transaction"
            )

    def _record_archive(self, path: Path, manifest: Mapping[str, Any]) -> _ArchiveRecord:
        self._require_private_directory(path)
        snapshot = manifest.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise archive.ArchiveError("archive_corrupt", "archive manifest lacks a snapshot")
        fingerprint = snapshot.get("fingerprint")
        if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
            raise archive.ArchiveError("archive_corrupt", "archive snapshot fingerprint is invalid")
        return _ArchiveRecord(
            identity=self._directory_identity(path),
            manifest_sha256=self._private_file_digest(path / "manifest.json"),
            artifacts=self._artifact_records(manifest),
            snapshot_fingerprint=fingerprint,
        )

    def _artifact_records(self, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise archive.ArchiveError("archive_corrupt", "archive artifact records are invalid")
        normalized: dict[str, dict[str, Any]] = {}
        for name, value in artifacts.items():
            if (
                not isinstance(name, str)
                or not self._safe_filename(name)
                or not isinstance(value, Mapping)
                or set(value) != {"sha256", "bytes"}
                or not isinstance(value.get("sha256"), str)
                or _SHA256.fullmatch(value["sha256"]) is None
                or not isinstance(value.get("bytes"), int)
                or isinstance(value["bytes"], bool)
                or value["bytes"] < 0
            ):
                raise archive.ArchiveError(
                    "archive_corrupt", "archive artifact records are invalid"
                )
            normalized[name] = {"sha256": value["sha256"], "bytes": value["bytes"]}
        return dict(sorted(normalized.items()))

    @staticmethod
    def _exclusion_summary(
        source_manifest: Mapping[str, Any], filtered_snapshot: Mapping[str, Any]
    ) -> tuple[int, int]:
        source_snapshot = source_manifest.get("snapshot")
        if not isinstance(source_snapshot, Mapping):
            raise archive.ArchiveError("archive_corrupt", "archive snapshot is invalid")
        source_entries = source_snapshot.get("entries")
        filtered_entries = filtered_snapshot.get("entries")
        source_bytes = source_snapshot.get("total_bytes")
        filtered_bytes = filtered_snapshot.get("total_bytes")
        if (
            not isinstance(source_entries, list)
            or not isinstance(filtered_entries, list)
            or not isinstance(source_bytes, int)
            or isinstance(source_bytes, bool)
            or not isinstance(filtered_bytes, int)
            or isinstance(filtered_bytes, bool)
        ):
            raise archive.ArchiveError("archive_corrupt", "archive snapshot is invalid")
        excluded_entries = len(source_entries) - len(filtered_entries)
        excluded_bytes = source_bytes - filtered_bytes
        if excluded_entries < 0 or excluded_bytes < 0:
            raise archive.ArchiveError("archive_corrupt", "archive snapshot is invalid")
        return excluded_entries, excluded_bytes

    def _new_journal(
        self, evidence: _Evidence, staging: Path, previous: Path
    ) -> dict[str, Any]:
        return {
            "schema": _JOURNAL_SCHEMA,
            "state": "prepared",
            "archive_path": str(evidence.source),
            "token": evidence.token,
            "token_payload": evidence.payload,
            "exclude_ignored_paths": list(evidence.policy),
            "source": evidence.source_record.to_dict(),
            "staging": staging.name,
            "previous": previous.name,
            "published": None,
        }

    def _evidence_from_journal(
        self, source: Path, journal: Mapping[str, Any]
    ) -> _Evidence:
        self._validate_journal(source, journal)
        payload = journal["token_payload"]
        assert isinstance(payload, dict)
        policy_value = journal["exclude_ignored_paths"]
        assert isinstance(policy_value, list)
        policy = tuple(policy_value)
        source_record = self._record_from_value(journal["source"])
        lease_id = payload["lease_id"]
        repository_id = payload["repository_id"]
        assert isinstance(lease_id, str)
        assert isinstance(repository_id, str)
        lease = self._validate_journal_lease(lease_id, repository_id)
        private_root = self._validate_private_namespace(
            source, lease, source.name, source_may_be_absent=True
        )
        token = self._token(payload)
        stored_token = journal["token"]
        assert isinstance(stored_token, str)
        if not hmac.compare_digest(token, stored_token):
            raise archive.ArchiveError(
                "repack_journal_invalid", "repack journal token is invalid"
            )
        return _Evidence(
            source=source,
            private_root=private_root,
            lease=lease,
            policy=policy,
            token=token,
            payload=payload,
            source_record=source_record,
            excluded_entries=None,
            excluded_bytes=None,
        )

    def _validate_journal(self, source: Path, journal: Mapping[str, Any]) -> None:
        required = {
            "schema",
            "state",
            "archive_path",
            "token",
            "token_payload",
            "exclude_ignored_paths",
            "source",
            "staging",
            "previous",
            "published",
        }
        if set(journal) != required or journal.get("schema") != _JOURNAL_SCHEMA:
            raise archive.ArchiveError("repack_journal_invalid", "repack journal shape is invalid")
        if journal.get("state") not in {"prepared", "source_moved", "published", "completed"}:
            raise archive.ArchiveError("repack_journal_invalid", "repack journal state is invalid")
        if journal.get("archive_path") != str(source):
            raise archive.ArchiveError("repack_journal_invalid", "repack journal belongs to another archive")
        if not isinstance(journal.get("token"), str) or _SHA256.fullmatch(journal["token"]) is None:
            raise archive.ArchiveError("repack_journal_invalid", "repack journal token is invalid")
        payload = journal.get("token_payload")
        if not isinstance(payload, dict) or not self._valid_token_payload(payload, source):
            raise archive.ArchiveError("repack_journal_invalid", "repack journal payload is invalid")
        policy = journal.get("exclude_ignored_paths")
        if (
            not isinstance(policy, list)
            or tuple(policy) != self._normalize_policy(tuple(policy))
            or payload["exclude_ignored_paths"] != policy
        ):
            raise archive.ArchiveError("repack_journal_invalid", "repack journal policy is invalid")
        source_record = self._record_from_value(journal.get("source"))
        if payload["source"] != source_record.to_dict():
            raise archive.ArchiveError("repack_journal_invalid", "repack journal source is invalid")
        staging = journal.get("staging")
        if not isinstance(staging, str) or not self._safe_sibling(source, staging, "staging"):
            raise archive.ArchiveError("repack_journal_invalid", "repack journal sibling is invalid")
        previous = journal.get("previous")
        if previous is not None and (
            not isinstance(previous, str)
            or not self._safe_sibling(source, previous, "previous")
        ):
            raise archive.ArchiveError("repack_journal_invalid", "repack journal sibling is invalid")
        if journal["state"] != "completed" and previous is None:
            raise archive.ArchiveError("repack_journal_invalid", "repack journal sibling is invalid")
        published = journal.get("published")
        if journal["state"] in {"published", "completed"}:
            self._record_from_value(published)
        elif published is not None:
            raise archive.ArchiveError("repack_journal_invalid", "repack journal publication is invalid")

    def _valid_token_payload(self, payload: Mapping[str, Any], source: Path) -> bool:
        required = {
            "schema",
            "archive_path",
            "repository_id",
            "lease_id",
            "source",
            "exclude_ignored_paths",
        }
        if set(payload) != required or payload.get("schema") != _TOKEN_SCHEMA:
            return False
        if (
            payload.get("archive_path") != str(source)
            or not isinstance(payload.get("repository_id"), str)
            or _SHA256.fullmatch(payload["repository_id"]) is None
            or not isinstance(payload.get("lease_id"), str)
            or not payload["lease_id"]
            or not isinstance(payload.get("exclude_ignored_paths"), list)
        ):
            return False
        try:
            policy = tuple(payload["exclude_ignored_paths"])
            return policy == self._normalize_policy(policy) and self._record_from_value(
                payload.get("source")
            ).to_dict() == payload.get("source")
        except (TypeError, ValueError, archive.ArchiveError):
            return False

    def _validate_journal_lease(self, lease_id: str, repository_id: str) -> Lease:
        try:
            lease = self.registry.get_lease_read_only(lease_id)
            reservation = self.registry.get_cleanup_reservation(lease_id)
            current_repository_id = self.git.repository_id()
            current_root = self.git.repository_root().resolve()
            current_name = self.git.repository_name()
        except sqlite3.Error as error:
            raise archive.ArchiveError(
                "registry_conflict", "unable to read the worktree registry"
            ) from error
        except (GitError, OSError, RuntimeError) as error:
            raise archive.ArchiveError(
                "repository_inspection_failed", "unable to inspect the current repository"
            ) from error
        if lease is None:
            raise archive.ArchiveError("unknown_lease", "archive lease no longer exists")
        if lease.state is not LeaseState.REMOVED:
            raise archive.ArchiveError(
                "archive_not_removed", "archive repack requires a removed worktree lease"
            )
        if reservation is not None:
            raise archive.ArchiveError(
                "cleanup_reserved", "archive repack cannot run while cleanup is reserved"
            )
        try:
            lease_root = lease.repository_root.resolve()
        except (OSError, RuntimeError) as error:
            raise archive.ArchiveError(
                "repository_mismatch", "lease repository root cannot be resolved"
            ) from error
        if (
            lease.repository_id != repository_id
            or current_repository_id != repository_id
            or lease.repository_name != current_name
            or lease_root != current_root
        ):
            raise archive.ArchiveError(
                "repository_mismatch", "archive does not belong to the current repository"
            )
        return lease

    def _read_journal_optional(self, source: Path) -> dict[str, Any] | None:
        path = self._journal_path(source)
        try:
            details = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise archive.ArchiveError(
                "repack_journal_invalid", "unable to inspect repack journal"
            ) from error
        self._require_private_file(path, details, maximum_bytes=_MAX_JOURNAL_BYTES)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise archive.ArchiveError(
                "repack_journal_invalid", "unable to read repack journal"
            ) from error
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(_MAX_JOURNAL_BYTES + 1)
            if len(payload) > _MAX_JOURNAL_BYTES:
                raise archive.ArchiveError("repack_journal_invalid", "repack journal is too large")
            value = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise archive.ArchiveError(
                "repack_journal_invalid", "repack journal is malformed"
            ) from error
        finally:
            os.close(descriptor)
        if not isinstance(value, dict):
            raise archive.ArchiveError("repack_journal_invalid", "repack journal is malformed")
        return value

    def _write_journal(self, path: Path, journal: Mapping[str, Any]) -> None:
        source_name = path.name.removeprefix(".awf-repack-").removesuffix(".json")
        self._validate_journal(path.parent / source_name, journal)
        self._require_private_directory(path.parent)
        try:
            payload = self._canonical_json(journal) + b"\n"
        except (TypeError, ValueError) as error:
            raise archive.ArchiveError(
                "repack_journal_invalid", "repack journal cannot be serialized"
            ) from error
        temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                _PRIVATE_FILE_MODE,
            )
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to create repack journal"
            ) from error
        try:
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("unable to write repack journal")
                remaining = remaining[written:]
            os.fsync(descriptor)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to sync repack journal"
            ) from error
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to publish repack journal"
            ) from error

    def _remove_journal(self, path: Path) -> None:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to inspect repack journal"
            ) from error
        self._require_private_file(path, details, maximum_bytes=_MAX_JOURNAL_BYTES)
        try:
            path.unlink()
            self._fsync_directory(path.parent)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to remove repack journal"
            ) from error

    def _delete_verified_archive(self, path: Path, expected: _ArchiveRecord) -> None:
        entries = self._validate_original_quarantine(path, expected)
        try:
            for entry in entries:
                entry.unlink()
                self._fsync_directory(path)
            path.rmdir()
            self._fsync_directory(path.parent)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to remove verified original archive"
            ) from error

    def _validate_original_quarantine(
        self, path: Path, expected: _ArchiveRecord
    ) -> tuple[Path, ...]:
        self._require_private_directory(path)
        if self._directory_identity(path) != expected.identity:
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "original archive directory changed during cleanup"
            )
        expected_files: dict[str, tuple[str, int | None]] = {
            "manifest.json": (expected.manifest_sha256, None)
        }
        for name, record in expected.artifacts.items():
            if name == "manifest.json":
                raise archive.ArchiveError(
                    "archive_recovery_mismatch", "original archive has an invalid artifact name"
                )
            expected_files[name] = (record["sha256"], record["bytes"])
        try:
            entries = tuple(sorted(path.iterdir(), key=lambda item: item.name))
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to inspect original archive for deletion"
            ) from error
        if not {entry.name for entry in entries}.issubset(expected_files):
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "original archive contains an unknown cleanup artifact"
            )
        for entry in entries:
            expected_hash, expected_bytes = expected_files[entry.name]
            self._verify_private_file_hash(
                entry, expected_hash=expected_hash, expected_bytes=expected_bytes
            )
        return entries

    def _verify_private_file_hash(
        self, path: Path, *, expected_hash: str, expected_bytes: int | None
    ) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "original archive artifact is unavailable"
            ) from error
        digest = hashlib.sha256()
        try:
            details = os.fstat(descriptor)
            self._require_private_file(path, details, maximum_bytes=None)
            if expected_bytes is not None and details.st_size != expected_bytes:
                raise archive.ArchiveError(
                    "archive_recovery_mismatch", "original archive artifact size changed"
                )
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "original archive artifact cannot be read"
            ) from error
        finally:
            os.close(descriptor)
        if not hmac.compare_digest(digest.hexdigest(), expected_hash):
            raise archive.ArchiveError(
                "archive_recovery_mismatch", "original archive artifact hash changed"
            )

    def _fsync_archive(self, path: Path, manifest: Mapping[str, Any]) -> None:
        for name in ("manifest.json", *self._artifact_records(manifest)):
            self._fsync_private_file(path / name)
        self._fsync_directory(path)
        self._fsync_directory(path.parent)

    def _fsync_private_file(self, path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to open archive artifact for sync"
            ) from error
        try:
            details = os.fstat(descriptor)
            self._require_private_file(path, details, maximum_bytes=None)
            os.fsync(descriptor)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to sync archive artifact"
            ) from error
        finally:
            os.close(descriptor)

    @staticmethod
    def _normalize_policy(paths: tuple[str, ...]) -> tuple[str, ...]:
        normalized: set[str] = set()
        for value in paths:
            if not isinstance(value, str) or not value or "\\" in value:
                raise ValueError("exclude path is invalid")
            candidate = PurePosixPath(value)
            if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
                raise ValueError("exclude path is not relative")
            rendered = candidate.as_posix()
            if rendered in {"", "."}:
                raise ValueError("exclude path is invalid")
            normalized.add(rendered)
        return tuple(sorted(normalized))

    def _canonical_parent_path(self, supplied: Path) -> Path:
        try:
            parent = supplied.parent.resolve(strict=True)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_identity_mismatch", "archive parent is unavailable"
            ) from error
        if parent != supplied.parent:
            raise archive.ArchiveError(
                "archive_identity_mismatch", "archive parent must be canonical"
            )
        self._require_private_directory(parent)
        return parent / supplied.name

    def _canonical_existing_archive(self, supplied: Path) -> Path:
        source = self._canonical_parent_path(supplied)
        try:
            resolved = source.resolve(strict=True)
        except OSError as error:
            raise archive.ArchiveError("archive_missing", "archive directory is unavailable") from error
        if resolved != source:
            raise archive.ArchiveError(
                "archive_identity_mismatch", "archive directory must be canonical"
            )
        self._require_private_directory(source)
        return source

    def _new_sibling(self, source: Path, kind: str) -> Path:
        prefix = f".{source.name}.awf-repack-{kind}-"
        for _ in range(16):
            candidate = source.parent / f"{prefix}{secrets.token_hex(12)}"
            if not self._exists(candidate):
                return candidate
        raise archive.ArchiveError(
            "archive_repack_failed", "unable to allocate a private repack staging path"
        )

    def _journal_sibling(self, source: Path, name: str, kind: str) -> Path:
        if not self._safe_sibling(source, name, kind):
            raise archive.ArchiveError("repack_journal_invalid", "repack journal sibling is invalid")
        return source.parent / name

    def _safe_sibling(self, source: Path, name: str, kind: str) -> bool:
        return (
            "/" not in name
            and name.startswith(f".{source.name}.awf-repack-{kind}-")
            and re.fullmatch(r"\.[0-9a-f]{64}\.awf-repack-" + kind + r"-[0-9a-f]{24}", name)
            is not None
        )

    @staticmethod
    def _journal_path(source: Path) -> Path:
        return source.parent / f".awf-repack-{source.name}.json"

    @staticmethod
    def _directory_identity(path: Path) -> dict[str, int]:
        try:
            details = path.lstat()
        except OSError as error:
            raise archive.ArchiveError("archive_missing", "archive directory is unavailable") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise archive.ArchiveError("archive_unsafe", "archive directory is unsafe")
        return {
            "device": details.st_dev,
            "inode": details.st_ino,
            "mode": stat.S_IMODE(details.st_mode),
            "uid": details.st_uid,
        }

    def _private_file_digest(self, path: Path) -> str:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise archive.ArchiveError("archive_corrupt", "archive manifest is unavailable") from error
        digest = hashlib.sha256()
        try:
            details = os.fstat(descriptor)
            self._require_private_file(path, details, maximum_bytes=archive.MAX_MANIFEST_BYTES)
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
        except OSError as error:
            raise archive.ArchiveError("archive_corrupt", "archive manifest cannot be read") from error
        finally:
            os.close(descriptor)
        return digest.hexdigest()

    @staticmethod
    def _require_private_directory(path: Path) -> None:
        try:
            details = path.lstat()
        except OSError as error:
            raise archive.ArchiveError("archive_unsafe", "private archive directory is unavailable") from error
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise archive.ArchiveError("archive_unsafe", "private archive directory is unsafe")

    @staticmethod
    def _require_private_file(
        path: Path, details: os.stat_result, *, maximum_bytes: int | None
    ) -> None:
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != _PRIVATE_FILE_MODE
            or (maximum_bytes is not None and details.st_size > maximum_bytes)
        ):
            raise archive.ArchiveError("archive_unsafe", "private archive file is unsafe")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to open archive directory for sync"
            ) from error
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to sync archive directory"
            ) from error
        finally:
            os.close(descriptor)

    @staticmethod
    def _rename(source: Path, destination: Path) -> None:
        try:
            os.rename(source, destination)
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to publish private archive directory"
            ) from error

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise archive.ArchiveError(
                "archive_repack_failed", "unable to inspect archive transaction path"
            ) from error
        return True

    @staticmethod
    def _safe_filename(name: str) -> bool:
        return name not in {"", ".", ".."} and "/" not in name and "\\" not in name

    @staticmethod
    def _canonical_json(value: Mapping[str, Any]) -> bytes:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    def _token(self, payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(self._canonical_json(payload)).hexdigest()

    def _record_from_value(self, value: Any) -> _ArchiveRecord:
        if not isinstance(value, Mapping) or set(value) != {
            "identity",
            "manifest_sha256",
            "artifacts",
            "snapshot_fingerprint",
        }:
            raise archive.ArchiveError("repack_journal_invalid", "archive record is invalid")
        identity = value.get("identity")
        if (
            not isinstance(identity, Mapping)
            or set(identity) != {"device", "inode", "mode", "uid"}
            or any(
                not isinstance(identity[key], int)
                or isinstance(identity[key], bool)
                or identity[key] < 0
                for key in identity
            )
        ):
            raise archive.ArchiveError("repack_journal_invalid", "archive identity is invalid")
        manifest_sha256 = value.get("manifest_sha256")
        snapshot_fingerprint = value.get("snapshot_fingerprint")
        if (
            not isinstance(manifest_sha256, str)
            or _SHA256.fullmatch(manifest_sha256) is None
            or not isinstance(snapshot_fingerprint, str)
            or _SHA256.fullmatch(snapshot_fingerprint) is None
        ):
            raise archive.ArchiveError("repack_journal_invalid", "archive hashes are invalid")
        artifacts_value = value.get("artifacts")
        if not isinstance(artifacts_value, Mapping) or not artifacts_value:
            raise archive.ArchiveError("repack_journal_invalid", "archive artifacts are invalid")
        artifacts: dict[str, dict[str, Any]] = {}
        for name, artifact in artifacts_value.items():
            if (
                not isinstance(name, str)
                or not self._safe_filename(name)
                or not isinstance(artifact, Mapping)
                or set(artifact) != {"sha256", "bytes"}
                or not isinstance(artifact.get("sha256"), str)
                or _SHA256.fullmatch(artifact["sha256"]) is None
                or not isinstance(artifact.get("bytes"), int)
                or isinstance(artifact["bytes"], bool)
                or artifact["bytes"] < 0
            ):
                raise archive.ArchiveError("repack_journal_invalid", "archive artifacts are invalid")
            artifacts[name] = {"sha256": artifact["sha256"], "bytes": artifact["bytes"]}
        return _ArchiveRecord(
            identity={key: identity[key] for key in ("device", "inode", "mode", "uid")},
            manifest_sha256=manifest_sha256,
            artifacts=dict(sorted(artifacts.items())),
            snapshot_fingerprint=snapshot_fingerprint,
        )

    def _preview_result(self, evidence: _Evidence) -> CommandResult:
        return CommandResult.ok(
            _COMMAND,
            decision="preview",
            lease=evidence.lease,
            actions=(
                {
                    "kind": "repack_archive_contents",
                    "archive_directory": str(evidence.source),
                    "preview_token": evidence.token,
                    "exclude_ignored_paths": list(evidence.policy),
                    "excluded_entries": evidence.excluded_entries,
                    "excluded_bytes": evidence.excluded_bytes,
                },
                {
                    "kind": "publish_repacked_archive",
                    "archive_directory": str(evidence.source),
                },
            ),
        )

    def _repacked(self, evidence: _Evidence, *, idempotent: bool) -> CommandResult:
        return CommandResult.ok(
            _COMMAND,
            decision="repacked",
            lease=evidence.lease,
            actions=(
                {
                    "kind": "repack_archive_contents",
                    "archive_directory": str(evidence.source),
                    "exclude_ignored_paths": list(evidence.policy),
                    "idempotent": idempotent,
                },
            ),
        )

    def _lock_path(self, source: Path) -> Path:
        digest = hashlib.sha256(os.fsencode(str(source))).hexdigest()
        return self.lock_dir / f"archive-repack-{digest}.lock"

    @staticmethod
    def _blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            _COMMAND, blockers=({"code": code, "message": message},), lease=lease
        )
