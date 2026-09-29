from __future__ import annotations

from collections.abc import Iterator

import io
import hashlib
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, BinaryIO, Protocol

from . import git_state
from .git import GitClient, GitError
from .git_state import GitStateError


_ARCHIVE_SCHEMA_VERSION = 2
_LEGACY_ARCHIVE_SCHEMA_VERSION = 1
_BASE_ARCHIVE_FILES = frozenset({"history.bundle", "manifest.json", "worktree.tar"})
_GIT_STATE_FILES = frozenset({"git-objects.pack", "git-state.tar"})
_CHUNK_SIZE = 1024 * 1024
_MANIFEST_FIXED_OVERHEAD = 16 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be created or validated safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_backup_root(
    root: Path, *, forbidden_roots: tuple[Path, ...]
) -> Path:
    """Validate a private, existing archive root without creating anything."""
    supplied = Path(root)
    if not supplied.is_absolute():
        raise ArchiveError("backup_root_invalid", "backup root must be an absolute path")
    if ".." in supplied.parts:
        raise ArchiveError("backup_root_invalid", "backup root must not contain '..'")

    _validate_directory_chain(supplied, label="backup root")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise ArchiveError("backup_root_invalid", "backup root is unavailable") from error
    if resolved != supplied:
        raise ArchiveError("backup_root_invalid", "backup root must be canonical")

    try:
        details = resolved.lstat()
    except OSError as error:
        raise ArchiveError("backup_root_invalid", "backup root is unavailable") from error
    if details.st_uid != os.getuid():
        raise ArchiveError("backup_root_unsafe", "backup root must be owned by this user")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise ArchiveError("backup_root_unsafe", "backup root must have mode 0700")

    for forbidden in forbidden_roots:
        candidate = Path(forbidden)
        try:
            if not candidate.is_absolute():
                candidate = candidate.absolute()
            resolved_forbidden = candidate.resolve(strict=False)
        except OSError as error:
            raise ArchiveError(
                "backup_root_invalid", "unable to inspect a forbidden archive location"
            ) from error
        if _paths_overlap(resolved, resolved_forbidden):
            raise ArchiveError(
                "backup_root_forbidden",
                "backup root must be outside repository and managed worktree locations",
            )

    return resolved


def snapshot_worktree(
    path: Path, *, exclude_ignored_paths: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Stream a complete non-Git worktree snapshot without following symlinks."""
    supplied = Path(path)
    try:
        root_details = supplied.lstat()
    except OSError as error:
        raise ArchiveError("worktree_unavailable", "worktree root is unavailable") from error
    if stat.S_ISLNK(root_details.st_mode) or not stat.S_ISDIR(root_details.st_mode):
        raise ArchiveError(
            "worktree_unsafe", "worktree root must be a non-symlink directory"
        )
    try:
        root = supplied.resolve(strict=True)
        root_details = root.lstat()
    except OSError as error:
        raise ArchiveError("worktree_unavailable", "worktree root is unavailable") from error
    if stat.S_ISLNK(root_details.st_mode) or not stat.S_ISDIR(root_details.st_mode):
        raise ArchiveError(
            "worktree_unsafe", "worktree root must be a non-symlink directory"
        )

    excluded_paths = _normalize_excluded_paths(exclude_ignored_paths)
    _validate_excluded_paths(root, excluded_paths)
    entries: list[dict[str, Any]] = [
        {
            "name": ".",
            "type": "directory",
            "mode": stat.S_IMODE(root_details.st_mode),
        }
    ]
    observed: dict[str, tuple[os.stat_result, str | None]] = {
        ".": (root_details, None)
    }
    total_bytes = 0

    try:
        for relative, entry, entry_details in _walk_worktree(root, excluded_paths):
            if Path(relative).name.casefold() == ".git":
                raise ArchiveError(
                    "nested_git_repository",
                    "nested Git repositories and submodules cannot be archived safely",
                )
            _require_utf8(relative, "worktree path")
            mode = stat.S_IMODE(entry_details.st_mode)
            if stat.S_ISDIR(entry_details.st_mode):
                observed[relative] = (entry_details, None)
                entries.append(
                    {"name": relative, "type": "directory", "mode": mode}
                )
                continue
            if stat.S_ISLNK(entry_details.st_mode):
                try:
                    target = os.readlink(entry.path)
                except OSError as error:
                    raise ArchiveError(
                        "snapshot_failed", f"unable to read symlink {relative!r}"
                    ) from error
                _require_utf8(target, "symlink target")
                observed[relative] = (entry_details, target)
                entries.append(
                    {
                        "name": relative,
                        "type": "symlink",
                        "mode": mode,
                        "link_target": target,
                    }
                )
                continue
            if stat.S_ISREG(entry_details.st_mode):
                digest, size = _digest_regular_file(
                    Path(entry.path), entry_details, relative
                )
                observed[relative] = (entry_details, None)
                entries.append(
                    {
                        "name": relative,
                        "type": "regular",
                        "mode": mode,
                        "size": size,
                        "hash": digest,
                    }
                )
                total_bytes += size
                continue
            raise ArchiveError(
                "unsupported_filesystem",
                f"worktree entry {relative!r} is not a regular file, directory, or symlink",
            )
    except ArchiveError:
        raise
    except OSError as error:
        raise ArchiveError("snapshot_failed", "unable to traverse worktree") from error

    _verify_observed_worktree(root, observed)
    entries.sort(key=lambda item: str(item["name"]))
    details: dict[str, Any] = {
        "entries": entries,
        "total_bytes": total_bytes,
        "root": {
            "device": root_details.st_dev,
            "inode": root_details.st_ino,
            "mode": stat.S_IMODE(root_details.st_mode),
        },
    }
    if excluded_paths:
        details["excluded_paths"] = list(excluded_paths)
    fingerprint = _sha256_bytes(_canonical_json_bytes(details, "snapshot_invalid"))
    result: dict[str, Any] = {
        "fingerprint": fingerprint,
        "entries": entries,
        "total_bytes": total_bytes,
        "root": details["root"],
    }
    if excluded_paths:
        result["excluded_paths"] = list(excluded_paths)
    return result


def create_archive(
    *,
    destination: Path,
    worktree_path: Path,
    snapshot: dict[str, Any],
    metadata: dict[str, Any],
    git: GitClient,
    git_state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a durable archive once, or fully verify an existing matching archive."""
    normalized_metadata = _normalize_metadata(metadata)
    normalized_snapshot = _normalize_snapshot(snapshot)
    normalized_git_state = _normalize_git_state_snapshot(git_state_snapshot)
    _manifest_byte_limit(
        normalized_metadata, normalized_snapshot, require_fit=True
    )
    archive_path = _archive_destination(destination)

    existing = _lstat_optional(archive_path)
    if existing is not None:
        return _verify_and_sync_archive(
            archive_path,
            expected_metadata=normalized_metadata,
            expected_snapshot=normalized_snapshot,
            git=git,
            git_state_snapshot=normalized_git_state,
        )

    current = snapshot_worktree(
        worktree_path,
        exclude_ignored_paths=tuple(normalized_snapshot.get("excluded_paths", ())),
    )
    if current != normalized_snapshot:
        raise ArchiveError(
            "snapshot_drift", "worktree changed before archive creation began"
        )

    try:
        archive_path.mkdir(mode=0o700)
    except FileExistsError:
        return _verify_and_sync_archive(
            archive_path,
            expected_metadata=normalized_metadata,
            expected_snapshot=normalized_snapshot,
            git=git,
            git_state_snapshot=normalized_git_state,
        )
    except OSError as error:
        raise ArchiveError("archive_create_failed", "unable to create archive directory") from error

    try:
        directory_details = archive_path.lstat()
        if (
            stat.S_ISLNK(directory_details.st_mode)
            or not stat.S_ISDIR(directory_details.st_mode)
            or directory_details.st_uid != os.getuid()
        ):
            raise ArchiveError("archive_unsafe", "archive directory is unsafe")
        os.chmod(archive_path, 0o700)
        _fsync_directory(archive_path.parent)

        bundle_path = archive_path / "history.bundle"
        git.create_bundle(bundle_path, cwd=worktree_path)
        _seal_private_file(bundle_path)

        tar_path = archive_path / "worktree.tar"
        tar_record = _write_worktree_tar(
            tar_path, Path(worktree_path), normalized_snapshot
        )

        git_state_artifacts: dict[str, Any] | None = None
        if normalized_git_state is not None:
            try:
                git_state_artifacts = _normalize_git_state_artifacts(
                    git_state.write_git_state(
                        git, Path(worktree_path), archive_path, normalized_git_state
                    )
                )
                _verify_git_state_artifacts(archive_path, git_state_artifacts)
            except ArchiveError:
                raise
            except (GitStateError, GitError, OSError) as error:
                raise ArchiveError(
                    "git_state_failed", "Git state could not be captured safely."
                ) from error

        final_snapshot = snapshot_worktree(
            worktree_path,
            exclude_ignored_paths=tuple(normalized_snapshot.get("excluded_paths", ())),
        )
        if final_snapshot != normalized_snapshot:
            raise ArchiveError(
                "snapshot_drift", "worktree changed while archive content was written"
            )

        artifacts: dict[str, Any] = {
            "history.bundle": _artifact_record(bundle_path),
            "worktree.tar": tar_record,
        }
        if git_state_artifacts is not None:
            artifacts.update(git_state_artifacts)
        manifest: dict[str, Any] = {
            "schema_version": _ARCHIVE_SCHEMA_VERSION,
            "metadata": normalized_metadata,
            "snapshot": normalized_snapshot,
            "artifacts": artifacts,
            "restore": _restore_instructions(
                normalized_metadata["head_sha"],
                portable_restore=normalized_git_state is not None,
            ),
        }
        if normalized_git_state is not None:
            manifest["git_state_snapshot"] = normalized_git_state
            manifest["git_state_artifacts"] = git_state_artifacts
        _write_private_json(archive_path / "manifest.json", manifest)
        _fsync_directory(archive_path)

        return _verify_and_sync_archive(
            archive_path,
            expected_metadata=normalized_metadata,
            expected_snapshot=normalized_snapshot,
            git=git,
            git_state_snapshot=normalized_git_state,
        )
    except ArchiveError:
        raise
    except GitStateError as error:
        raise ArchiveError(
            error.code, "Git state could not be captured safely."
        ) from error
    except GitError as error:
        raise ArchiveError("archive_bundle_failed", "unable to create history bundle") from error
    except RuntimeError as error:
        raise ArchiveError(
            "archive_git_state_failed", "unable to create Git state archive artifacts"
        ) from error
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError("archive_write_failed", "unable to write archive safely") from error


def verify_archive(
    destination: Path,
    *,
    expected_metadata: dict[str, Any],
    expected_snapshot: dict[str, Any],
    git: GitClient,
    git_state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify every archive artifact without depending on the source repository."""
    normalized_metadata = _normalize_metadata(expected_metadata)
    normalized_snapshot = _normalize_snapshot(expected_snapshot)
    normalized_git_state = _normalize_git_state_snapshot(git_state_snapshot)
    archive_path = _archive_destination(destination)
    manifest = _read_verified_archive(archive_path, git=git)
    manifest_metadata = _normalize_metadata_value(manifest.get("metadata"))
    manifest_snapshot = _normalize_snapshot_value(manifest.get("snapshot"))
    if manifest_metadata != normalized_metadata:
        raise ArchiveError(
            "archive_metadata_mismatch", "archive metadata does not match this discard request"
        )
    if not _snapshots_match(
        manifest_snapshot,
        normalized_snapshot,
        legacy=manifest["schema_version"] == _LEGACY_ARCHIVE_SCHEMA_VERSION,
    ):
        raise ArchiveError(
            "archive_snapshot_mismatch", "archive snapshot does not match this worktree"
        )
    manifest_git_state = manifest.get("git_state_snapshot")
    if normalized_git_state is not None and manifest_git_state != normalized_git_state:
        raise ArchiveError(
            "archive_git_state_mismatch",
            "archive Git state does not match this discard request",
        )
    return manifest


def _verify_and_sync_archive(
    archive_path: Path,
    *,
    expected_metadata: dict[str, Any],
    expected_snapshot: dict[str, Any],
    git: GitClient,
    git_state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = verify_archive(
        archive_path,
        expected_metadata=expected_metadata,
        expected_snapshot=expected_snapshot,
        git=git,
        git_state_snapshot=git_state_snapshot,
    )
    for name in {"manifest.json", *manifest["artifacts"]}:
        _fsync_private_file(archive_path / name)
    _fsync_directory(archive_path)
    _fsync_directory(archive_path.parent)
    return manifest

def read_verified_archive(path: Path) -> dict[str, Any]:
    """Read and independently verify an archive without its source repository."""
    archive_path = _archive_source_path(path)
    return _read_verified_archive(archive_path, git=GitClient(archive_path))


def restore_archive(
    *,
    archive_path: Path,
    destination: Path,
) -> dict[str, Any]:
    """Restore one verified archive into an absent private destination."""
    source = _archive_source_path(archive_path)
    manifest = _read_verified_archive(source, git=GitClient(source))
    restored_snapshot = _normalize_snapshot_value(manifest["snapshot"])
    source_exclusions = tuple(restored_snapshot.get("excluded_paths", ()))
    restored_destination = validate_restore_destination(destination)

    with TemporaryDirectory(
        prefix=f".{restored_destination.name}.restore-",
        dir=restored_destination.parent,
    ) as temporary:
        staging = Path(temporary) / "worktree"
        try:
            staging.mkdir(mode=0o700)
            _restore_history_bundle(
                source / "history.bundle",
                staging,
                _normalize_metadata_value(manifest["metadata"])["head_sha"],
            )
            directory_modes = _restore_worktree_tar(
                source / "worktree.tar",
                staging,
                restored_snapshot,
                excluded_paths=source_exclusions,
                source_snapshot=restored_snapshot,
            )
            git_state_snapshot = manifest.get("git_state_snapshot")
            if git_state_snapshot is not None:
                try:
                    git_state.restore_git_state(source, staging, git_state_snapshot)
                except (GitStateError, GitError, OSError) as error:
                    raise ArchiveError(
                        "archive_corrupt",
                        "Git state artifacts cannot be independently verified or restored.",
                    ) from error
            _fsync_directory(staging)
            _restore_directory_modes(directory_modes)
            if _lstat_optional(restored_destination) is not None:
                raise ArchiveError(
                    "restore_destination_exists",
                    "restore destination was created while restoration was running",
                )
            os.replace(staging, restored_destination)
            _fsync_directory(restored_destination.parent)
        except ArchiveError:
            raise
        except GitStateError as error:
            raise ArchiveError(
                "archive_corrupt",
                "Git state artifacts cannot be independently verified or restored.",
            ) from error
        except (GitError, OSError, RuntimeError, tarfile.TarError) as error:
            raise ArchiveError(
                "archive_restore_failed", "unable to restore verified archive"
            ) from error

    return _restore_summary(
        source,
        restored_destination,
        manifest,
        restored_snapshot,
        git_state_restored=manifest.get("git_state_snapshot") is not None,
    )


def prepare_repack_snapshot(
    *,
    archive_path: Path,
    source_manifest: dict[str, Any],
    exclude_ignored_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Validate an archive repack policy and return its filtered snapshot."""
    source_path = _archive_source_path(archive_path)
    source_snapshot = _normalize_snapshot_value(source_manifest.get("snapshot"))
    requested_exclusions = _normalize_excluded_paths(exclude_ignored_paths)
    source_exclusions = tuple(source_snapshot.get("excluded_paths", ()))
    additions = tuple(
        path for path in requested_exclusions if path not in source_exclusions
    )
    if not additions:
        raise ArchiveError(
            "excluded_path_invalid",
            "repack exclusion policy does not remove any additional archive content",
        )
    _validate_archived_excluded_paths(source_path, source_manifest, additions)
    effective_exclusions = _normalize_excluded_paths(
        [*source_exclusions, *requested_exclusions]
    )
    return _filtered_snapshot(source_snapshot, effective_exclusions)


def repack_archive_contents(
    *,
    source: Path,
    destination: Path,
    exclude_ignored_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Create a verified derivative archive without changing the source archive."""
    source_path = _archive_source_path(source)
    destination_path = _archive_destination(destination)
    if _paths_overlap(source_path, destination_path):
        raise ArchiveError(
            "archive_destination_invalid",
            "repack destination must not overlap the source archive",
        )
    if _lstat_optional(destination_path) is not None:
        raise ArchiveError(
            "archive_destination_exists", "repack destination already exists"
        )
    source_manifest = _read_verified_archive(source_path, git=GitClient(source_path))
    filtered_snapshot = prepare_repack_snapshot(
        archive_path=source_path,
        source_manifest=source_manifest,
        exclude_ignored_paths=exclude_ignored_paths,
    )
    source_snapshot: dict[str, Any] = source_manifest["snapshot"]
    source_manifest_hash, _ = _file_digest(source_path / "manifest.json")
    normalized_metadata = _normalize_metadata_value(source_manifest["metadata"])

    try:
        destination_path.mkdir(mode=0o700)
        os.chmod(destination_path, 0o700)
        _fsync_directory(destination_path.parent)
        artifacts: dict[str, Any] = {
            "history.bundle": _copy_private_artifact(
                source_path / "history.bundle", destination_path / "history.bundle"
            ),
            "worktree.tar": _write_filtered_worktree_tar(
                source_path / "worktree.tar",
                destination_path / "worktree.tar",
                source_snapshot,
                filtered_snapshot,
            ),
        }
        if artifacts["history.bundle"] != source_manifest["artifacts"]["history.bundle"]:
            raise ArchiveError(
                "archive_corrupt", "copied history bundle does not match source"
            )
        git_state_snapshot = source_manifest.get("git_state_snapshot")
        git_state_artifacts = source_manifest.get("git_state_artifacts")
        if git_state_snapshot is not None:
            normalized_git_state = _normalize_git_state_snapshot(git_state_snapshot)
            normalized_git_state_artifacts = _normalize_git_state_artifacts(
                git_state_artifacts
            )
            for name in sorted(_GIT_STATE_FILES):
                artifacts[name] = _copy_private_artifact(
                    source_path / name, destination_path / name
                )
            if {
                name: artifacts[name] for name in _GIT_STATE_FILES
            } != normalized_git_state_artifacts:
                raise ArchiveError(
                    "archive_corrupt", "copied Git state artifacts do not match source"
                )
        else:
            normalized_git_state = None
            normalized_git_state_artifacts = None

        manifest: dict[str, Any] = {
            "schema_version": _ARCHIVE_SCHEMA_VERSION,
            "metadata": normalized_metadata,
            "snapshot": filtered_snapshot,
            "artifacts": artifacts,
            "restore": _restore_instructions(
                normalized_metadata["head_sha"], portable_restore=True
            ),
            "derivative": {
                "excluded_paths": filtered_snapshot["excluded_paths"],
                "source_manifest_sha256": source_manifest_hash,
                "source_snapshot_fingerprint": source_snapshot["fingerprint"],
            },
        }
        if normalized_git_state is not None:
            manifest["git_state_snapshot"] = normalized_git_state
            manifest["git_state_artifacts"] = normalized_git_state_artifacts
        _write_private_json(destination_path / "manifest.json", manifest)
        _fsync_directory(destination_path)
        verified = _read_verified_archive(destination_path, git=GitClient(destination_path))
        _fsync_archive(destination_path, verified)
        return verified
    except ArchiveError:
        raise
    except (OSError, RuntimeError, tarfile.TarError) as error:
        raise ArchiveError(
            "archive_repack_failed", "unable to write verified derivative archive"
        ) from error




def _archive_destination(destination: Path) -> Path:
    supplied = Path(destination)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise ArchiveError(
            "archive_destination_invalid", "archive destination must be an absolute direct child"
        )
    if supplied.name in {"", ".", ".."}:
        raise ArchiveError("archive_destination_invalid", "archive destination name is invalid")
    parent = validate_backup_root(supplied.parent, forbidden_roots=())
    return parent / supplied.name


def _validate_directory_chain(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise ArchiveError("backup_root_invalid", f"{label} must be absolute")
    current = Path(path.anchor)
    try:
        root_details = current.lstat()
    except OSError as error:
        raise ArchiveError("backup_root_invalid", f"{label} root is unavailable") from error
    _validate_directory_component(root_details, label)

    for part in path.parts[1:]:
        if part in {"", ".", ".."}:
            raise ArchiveError("backup_root_invalid", f"{label} path is not canonical")
        current /= part
        try:
            details = current.lstat()
        except OSError as error:
            raise ArchiveError("backup_root_invalid", f"{label} is unavailable") from error
        _validate_directory_component(details, label)


def _validate_directory_component(details: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise ArchiveError("backup_root_unsafe", f"{label} has a symlink or non-directory ancestor")
    if details.st_uid not in {0, os.getuid()}:
        raise ArchiveError("backup_root_unsafe", f"{label} has an untrusted owner")
    # A sticky ancestor (for example /tmp, mode 1777) lets other users create entries
    # but not rename or remove entries they do not own, so our owned descendants
    # cannot be swapped. Any other group- or world-writable ancestor is unsafe.
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not details.st_mode & stat.S_ISVTX:
        raise ArchiveError("backup_root_unsafe", f"{label} has an unsafe writable ancestor")


def _walk_worktree(
    root: Path, excluded_paths: tuple[str, ...]
) -> Iterator[tuple[str, os.DirEntry[str], os.stat_result]]:
    excluded = set(excluded_paths)

    def walk(directory: Path, relative_directory: str) -> Iterator[
        tuple[str, os.DirEntry[str], os.stat_result]
    ]:
        try:
            before = directory.lstat()
        except OSError as error:
            raise ArchiveError("snapshot_drift", "worktree directory changed during snapshot") from error
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise ArchiveError("snapshot_drift", "worktree directory changed during snapshot")
        try:
            with os.scandir(directory) as scanner:
                children = sorted(scanner, key=lambda item: item.name)
        except OSError as error:
            raise ArchiveError("snapshot_failed", "unable to enumerate worktree") from error
        for child in children:
            if not relative_directory and child.name == ".git":
                continue
            relative = child.name if not relative_directory else f"{relative_directory}/{child.name}"
            if relative in excluded:
                continue
            try:
                details = child.stat(follow_symlinks=False)
            except OSError as error:
                raise ArchiveError(
                    "snapshot_drift", f"worktree entry {relative!r} changed during snapshot"
                ) from error
            yield relative, child, details
            if stat.S_ISDIR(details.st_mode):
                yield from walk(Path(child.path), relative)
        try:
            after = directory.lstat()
        except OSError as error:
            raise ArchiveError("snapshot_drift", "worktree directory changed during snapshot") from error
        if not _same_directory_snapshot(before, after):
            raise ArchiveError("snapshot_drift", "worktree directory changed during snapshot")

    yield from walk(root, "")


def _digest_regular_file(
    path: Path, expected: os.stat_result, relative: str
) -> tuple[str, int]:
    _require_regular_identity(path, expected, relative)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArchiveError(
            "snapshot_drift", f"worktree file {relative!r} changed during snapshot"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not _same_regular_identity(expected, opened):
            raise ArchiveError(
                "snapshot_drift", f"worktree file {relative!r} changed during snapshot"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(descriptor, _CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
    except ArchiveError:
        raise
    except OSError as error:
        raise ArchiveError(
            "snapshot_failed", f"unable to read worktree file {relative!r}"
        ) from error
    finally:
        os.close(descriptor)
    try:
        final = path.lstat()
    except OSError as error:
        raise ArchiveError(
            "snapshot_drift", f"worktree file {relative!r} changed during snapshot"
        ) from error
    if not _same_regular_identity(expected, final) or size != expected.st_size:
        raise ArchiveError(
            "snapshot_drift", f"worktree file {relative!r} changed during snapshot"
        )
    return digest.hexdigest(), size


def _require_regular_identity(path: Path, expected: os.stat_result, relative: str) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise ArchiveError(
            "snapshot_drift", f"worktree file {relative!r} changed during snapshot"
        ) from error
    if not _same_regular_identity(expected, current):
        raise ArchiveError(
            "snapshot_drift", f"worktree file {relative!r} changed during snapshot"
        )


def _same_regular_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _same_directory_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _same_symlink_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISLNK(left.st_mode)
        and stat.S_ISLNK(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _verify_observed_worktree(
    root: Path, observed: dict[str, tuple[os.stat_result, str | None]]
) -> None:
    for name, (before, link_target) in observed.items():
        source = _source_path(root, name)
        try:
            current = source.lstat()
        except OSError as error:
            raise ArchiveError(
                "snapshot_drift", f"worktree entry {name!r} changed during snapshot"
            ) from error
        if stat.S_ISDIR(before.st_mode):
            unchanged = _same_directory_snapshot(before, current)
        elif stat.S_ISREG(before.st_mode):
            unchanged = _same_regular_identity(before, current)
        else:
            unchanged = _same_symlink_identity(before, current)
        if not unchanged:
            raise ArchiveError(
                "snapshot_drift", f"worktree entry {name!r} changed during snapshot"
            )
        if link_target is not None:
            try:
                current_target = os.readlink(source)
            except OSError as error:
                raise ArchiveError(
                    "snapshot_drift", f"worktree symlink {name!r} changed during snapshot"
                ) from error
            if current_target != link_target:
                raise ArchiveError(
                    "snapshot_drift", f"worktree symlink {name!r} changed during snapshot"
                )


def _normalize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return _normalize_metadata_value(metadata)


def _normalize_metadata_value(value: Any) -> dict[str, Any]:
    normalized = _json_copy(value, "metadata_invalid")
    if not isinstance(normalized, dict):
        raise ArchiveError("metadata_invalid", "archive metadata must be an object")
    required = {"head_sha", "lease", "preview_token", "reason", "repository_id"}
    if not required.issubset(normalized):
        raise ArchiveError("metadata_invalid", "archive metadata is incomplete")
    if not isinstance(normalized["head_sha"], str) or not _is_object_id(
        normalized["head_sha"]
    ):
        raise ArchiveError("metadata_invalid", "archive metadata has an invalid HEAD")
    for key in ("repository_id", "reason", "preview_token"):
        if not isinstance(normalized[key], str) or not normalized[key]:
            raise ArchiveError("metadata_invalid", f"archive metadata has an invalid {key}")
    if not isinstance(normalized["lease"], dict):
        raise ArchiveError("metadata_invalid", "archive metadata has an invalid lease")
    return normalized


def _normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _normalize_snapshot_value(snapshot)


def _normalize_snapshot_value(value: Any) -> dict[str, Any]:
    normalized = _json_copy(value, "snapshot_invalid")
    if not isinstance(normalized, dict):
        raise ArchiveError("snapshot_invalid", "worktree snapshot has an invalid shape")
    has_exclusion_policy = "excluded_paths" in normalized
    expected_keys = {
        "entries",
        "fingerprint",
        "root",
        "total_bytes",
        *(("excluded_paths",) if has_exclusion_policy else ()),
    }
    if set(normalized) != expected_keys:
        raise ArchiveError("snapshot_invalid", "worktree snapshot has an invalid shape")
    fingerprint = normalized["fingerprint"]
    if not isinstance(fingerprint, str) or not _is_sha256(fingerprint):
        raise ArchiveError("snapshot_invalid", "worktree snapshot has an invalid fingerprint")
    entries = normalized["entries"]
    total_bytes = normalized["total_bytes"]
    root = normalized["root"]
    if not isinstance(entries, list) or not _is_nonnegative_int(total_bytes):
        raise ArchiveError("snapshot_invalid", "worktree snapshot has invalid entries")
    if (
        not isinstance(root, dict)
        or set(root) != {"device", "inode", "mode"}
        or not all(_is_nonnegative_int(root[key]) for key in ("device", "inode", "mode"))
        or root["mode"] > 0o7777
    ):
        raise ArchiveError("snapshot_invalid", "worktree snapshot has an invalid root")

    excluded_paths: tuple[str, ...] = ()
    if has_exclusion_policy:
        raw_excluded_paths = normalized["excluded_paths"]
        if not isinstance(raw_excluded_paths, list):
            raise ArchiveError(
                "snapshot_invalid", "worktree snapshot has an invalid exclusion policy"
            )
        excluded_paths = _normalize_excluded_paths(raw_excluded_paths)
        if raw_excluded_paths != list(excluded_paths):
            raise ArchiveError(
                "snapshot_invalid", "worktree snapshot exclusion policy is not canonical"
            )

    names: set[str] = set()
    expected_total = 0
    previous_name: str | None = None
    for entry in entries:
        _validate_snapshot_entry(entry)
        name = entry["name"]
        if name in names or (previous_name is not None and name <= previous_name):
            raise ArchiveError("snapshot_invalid", "worktree snapshot entries are not sorted")
        names.add(name)
        previous_name = name
        if entry["type"] == "regular":
            expected_total += entry["size"]
    if "." not in names or next(
        entry for entry in entries if entry["name"] == "."
    )["type"] != "directory":
        raise ArchiveError("snapshot_invalid", "worktree snapshot lacks its root directory")
    entries_by_name = {entry["name"]: entry for entry in entries}
    for entry in entries:
        name = entry["name"]
        if name == ".":
            continue
        parent_name = PurePosixPath(name).parent.as_posix()
        parent = entries_by_name.get(parent_name)
        if parent is None or parent["type"] != "directory":
            raise ArchiveError(
                "snapshot_invalid",
                "worktree snapshot entry lacks a directory parent",
            )
    if total_bytes != expected_total:
        raise ArchiveError("snapshot_invalid", "worktree snapshot byte count is invalid")
    if excluded_paths and any(
        _is_excluded_entry(name, excluded_paths) for name in names
    ):
        raise ArchiveError(
            "snapshot_invalid",
            "worktree snapshot contains an excluded path",
        )

    details: dict[str, Any] = {
        "entries": entries,
        "total_bytes": total_bytes,
        "root": root,
    }
    if has_exclusion_policy:
        details["excluded_paths"] = list(excluded_paths)
    if fingerprint != _sha256_bytes(_canonical_json_bytes(details, "snapshot_invalid")):
        raise ArchiveError("snapshot_invalid", "worktree snapshot fingerprint is invalid")
    result: dict[str, Any] = {
        "fingerprint": fingerprint,
        "entries": entries,
        "total_bytes": total_bytes,
        "root": root,
    }
    if has_exclusion_policy:
        result["excluded_paths"] = list(excluded_paths)
    return result


def _snapshots_match(
    left: dict[str, Any], right: dict[str, Any], *, legacy: bool
) -> bool:
    if not legacy:
        return left == right
    return all(
        left[key] == right[key] for key in ("entries", "root", "total_bytes")
    )


def _normalize_excluded_paths(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(paths, str):
        raise ArchiveError("excluded_path_invalid", "excluded paths must be a path sequence")
    normalized_paths: set[str] = set()
    for raw_path in paths:
        if not isinstance(raw_path, str) or not raw_path:
            raise ArchiveError("excluded_path_invalid", "excluded path is invalid")
        _require_utf8(raw_path, "excluded path")
        candidate = PurePosixPath(raw_path)
        if (
            candidate.is_absolute()
            or "\\" in raw_path
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ArchiveError("excluded_path_invalid", "excluded path is not relative")
        normalized = candidate.as_posix()
        if normalized != "node_modules":
            raise ArchiveError(
                "excluded_path_invalid",
                "only the ignored node_modules directory may be excluded",
            )
        normalized_paths.add(normalized)
    return tuple(sorted(normalized_paths))


def _validate_excluded_paths(root: Path, excluded_paths: tuple[str, ...]) -> None:
    if not excluded_paths:
        return
    for relative_path in excluded_paths:
        candidate = root
        for component in PurePosixPath(relative_path).parts:
            candidate /= component
            try:
                details = candidate.lstat()
            except OSError as error:
                raise ArchiveError(
                    "excluded_path_unsafe", "excluded path is unavailable"
                ) from error
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise ArchiveError(
                    "excluded_path_unsafe",
                    "excluded path and its ancestors must be non-symlink directories",
                )
        try:
            ignored = _archived_path_is_ignored(root, relative_path)
            tracked = _archived_tracked_paths(root, relative_path)
        except GitError as error:
            raise ArchiveError(
                "excluded_path_unverified", "unable to verify ignored excluded path"
            ) from error
        if not ignored:
            raise ArchiveError(
                "excluded_path_unverified", "excluded path is not Git-ignored"
            )
        if tracked:
            raise ArchiveError(
                "excluded_path_unverified",
                "excluded path contains Git-tracked descendants",
            )


def _normalize_git_state_snapshot(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    normalized = _json_copy(value, "git_state_invalid")
    if (
        not isinstance(normalized, dict)
        or not isinstance(normalized.get("fingerprint"), str)
        or not normalized["fingerprint"]
    ):
        raise ArchiveError("git_state_invalid", "Git state snapshot lacks a fingerprint")
    return normalized


def _normalize_git_state_artifacts(value: Any) -> dict[str, Any]:
    normalized = _json_copy(value, "git_state_invalid")
    if not isinstance(normalized, dict) or set(normalized) != _GIT_STATE_FILES:
        raise ArchiveError("git_state_invalid", "Git state artifact records are invalid")
    for record in normalized.values():
        _validate_artifact_record(record)
    return normalized


def _verify_git_state_artifacts(
    archive_path: Path, artifacts: dict[str, Any]
) -> None:
    for name, record in artifacts.items():
        _verify_artifact_record(archive_path / name, record)


def _validate_snapshot_entry(value: Any) -> None:
    if not isinstance(value, dict):
        raise ArchiveError("snapshot_invalid", "worktree snapshot entry is invalid")
    entry_type_value = value.get("type")
    if not isinstance(entry_type_value, str):
        raise ArchiveError("snapshot_invalid", "worktree snapshot entry has an invalid type")
    entry_type = entry_type_value
    expected_fields = {
        "directory": {"name", "type", "mode"},
        "regular": {"hash", "mode", "name", "size", "type"},
        "symlink": {"link_target", "mode", "name", "type"},
    }.get(entry_type)
    if expected_fields is None or set(value) != expected_fields:
        raise ArchiveError("snapshot_invalid", "worktree snapshot entry has an invalid shape")
    name = value["name"]
    if not isinstance(name, str) or not _safe_archive_name(name):
        raise ArchiveError("snapshot_invalid", "worktree snapshot entry has an invalid name")
    _require_utf8(name, "worktree path")
    if not _is_nonnegative_int(value["mode"]) or value["mode"] > 0o7777:
        raise ArchiveError("snapshot_invalid", "worktree snapshot entry has an invalid mode")
    if entry_type == "regular":
        if not _is_nonnegative_int(value["size"]) or not _is_sha256(value["hash"]):
            raise ArchiveError("snapshot_invalid", "worktree snapshot file entry is invalid")
    elif entry_type == "symlink":
        target = value["link_target"]
        if not isinstance(target, str) or "\0" in target:
            raise ArchiveError("snapshot_invalid", "worktree snapshot symlink entry is invalid")
        _require_utf8(target, "symlink target")


def _safe_archive_name(name: str) -> bool:
    if name == ".":
        return True
    if not name or name.startswith("/") or "\0" in name:
        return False
    return all(
        component not in {"", ".", ".."} and component.casefold() != ".git"
        for component in name.split("/")
    )


def _write_worktree_tar(
    path: Path, root: Path, snapshot: dict[str, Any]
) -> dict[str, Any]:
    descriptor = _open_private_output(path)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            with _HashingWriter(output) as writer:
                with tarfile.open(
                    fileobj=writer, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    for entry in snapshot["entries"]:
                        source = _source_path(root, entry["name"])
                        _write_tar_entry(archive, source, entry)
                writer.flush()
                os.fsync(output.fileno())
    except ArchiveError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError("archive_write_failed", "unable to write worktree archive") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _seal_private_file(path)
    return {"sha256": writer.hexdigest, "bytes": writer.count}


def _source_path(root: Path, name: str) -> Path:
    if name == ".":
        return root
    return root.joinpath(*name.split("/"))


def _write_tar_entry(archive: tarfile.TarFile, source: Path, entry: dict[str, Any]) -> None:
    try:
        details = source.lstat()
    except OSError as error:
        raise ArchiveError(
            "snapshot_drift", f"worktree entry {entry['name']!r} changed during archive creation"
        ) from error
    _assert_entry_matches(entry, details, source)
    info = _tar_info(entry)
    if entry["type"] in {"directory", "symlink"}:
        archive.addfile(info)
        return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ArchiveError(
            "snapshot_drift", f"worktree file {entry['name']!r} changed during archive creation"
        ) from error
    try:
        opened = os.fstat(descriptor)
        _assert_entry_matches(entry, opened, source)
        with os.fdopen(descriptor, "rb", closefd=True) as input_file:
            descriptor = None
            reader = _HashingReader(input_file)
            archive.addfile(info, reader)
        try:
            final = source.lstat()
        except OSError as error:
            raise ArchiveError(
                "snapshot_drift", f"worktree file {entry['name']!r} changed during archive creation"
            ) from error
        _assert_entry_matches(entry, final, source)
        if reader.count != entry["size"] or reader.hexdigest != entry["hash"]:
            raise ArchiveError(
                "snapshot_drift", f"worktree file {entry['name']!r} changed during archive creation"
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_entry_matches(
    entry: dict[str, Any], details: os.stat_result, source: Path
) -> None:
    entry_type = entry["type"]
    matches_type = (
        (entry_type == "directory" and stat.S_ISDIR(details.st_mode))
        or (entry_type == "regular" and stat.S_ISREG(details.st_mode))
        or (entry_type == "symlink" and stat.S_ISLNK(details.st_mode))
    )
    if not matches_type or stat.S_IMODE(details.st_mode) != entry["mode"]:
        raise ArchiveError(
            "snapshot_drift", f"worktree entry {entry['name']!r} changed during archive creation"
        )
    if entry_type == "regular" and details.st_size != entry["size"]:
        raise ArchiveError(
            "snapshot_drift", f"worktree file {entry['name']!r} changed during archive creation"
        )
    if entry_type == "symlink":
        try:
            target = os.readlink(source)
        except OSError as error:
            raise ArchiveError(
                "snapshot_drift", f"worktree symlink {entry['name']!r} changed during archive creation"
            ) from error
        if target != entry["link_target"]:
            raise ArchiveError(
                "snapshot_drift", f"worktree symlink {entry['name']!r} changed during archive creation"
            )


def _verify_worktree_tar(path: Path, snapshot: dict[str, Any]) -> None:
    expected = {entry["name"]: entry for entry in snapshot["entries"]}
    seen: set[str] = set()
    try:
        with tarfile.open(path, mode="r:") as archive:
            while True:
                member = archive.next()
                if member is None:
                    break
                if member.name in seen or member.name not in expected:
                    raise ArchiveError("archive_corrupt", "worktree archive has unexpected entries")
                seen.add(member.name)
                _verify_tar_member(archive, member, expected[member.name])
    except ArchiveError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError("archive_corrupt", "worktree archive cannot be read") from error
    if seen != set(expected):
        raise ArchiveError("archive_corrupt", "worktree archive is incomplete")


def _verify_tar_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, expected: dict[str, Any]
) -> None:
    _assert_tar_member_shape(member, expected)
    if expected["type"] != "regular":
        return
    try:
        source = archive.extractfile(member)
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError("archive_corrupt", "worktree archive file cannot be read") from error
    if source is None:
        raise ArchiveError("archive_corrupt", "worktree archive file is unavailable")
    with source:
        digest = hashlib.sha256()
        size = 0
        while True:
            block = source.read(_CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
    if size != expected["size"] or digest.hexdigest() != expected["hash"]:
        raise ArchiveError("archive_corrupt", "worktree archive file checksum is invalid")


def _read_manifest(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    _validate_private_file(path)
    limit = min(maximum_bytes, MAX_MANIFEST_BYTES)
    try:
        if path.stat().st_size > limit:
            raise ArchiveError("archive_manifest_too_large", "archive manifest exceeds size limit")
        with path.open("rb") as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ArchiveError("archive_manifest_too_large", "archive manifest exceeds size limit")
        value = json.loads(raw.decode("utf-8"))
    except ArchiveError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveError("archive_corrupt", "archive manifest cannot be read") from error
    if not isinstance(value, dict):
        raise ArchiveError("archive_corrupt", "archive manifest is not an object")
    return value
def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    payload = _canonical_json_bytes(value, "archive_write_failed") + b"\n"
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ArchiveError("archive_manifest_too_large", "archive manifest exceeds size limit")
    descriptor = _open_private_output(path)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to write archive manifest") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _seal_private_file(path)


def _open_private_output(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ArchiveError("archive_incomplete", "archive artifact already exists") from error
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to create archive artifact") from error
    try:
        os.fchmod(descriptor, 0o600)
    except OSError as error:
        os.close(descriptor)
        raise ArchiveError("archive_write_failed", "unable to secure archive artifact") from error
    return descriptor


def _seal_private_file(path: Path) -> None:
    try:
        before = path.lstat()
    except OSError as error:
        raise ArchiveError("archive_write_failed", "archive artifact is unavailable") from error
    if not stat.S_ISREG(before.st_mode):
        raise ArchiveError("archive_unsafe", "archive artifact is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to secure archive artifact") from error
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise ArchiveError("archive_unsafe", "archive artifact changed unexpectedly")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except ArchiveError:
        raise
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to sync archive artifact") from error
    finally:
        os.close(descriptor)
    _validate_private_file(path)


def _validate_archive_layout(destination: Path) -> set[str]:
    try:
        details = destination.lstat()
    except FileNotFoundError as error:
        raise ArchiveError("archive_incomplete", "archive directory is missing") from error
    except OSError as error:
        raise ArchiveError("archive_corrupt", "archive directory is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise ArchiveError("archive_unsafe", "archive directory is not private")
    try:
        with os.scandir(destination) as scanner:
            names = {entry.name for entry in scanner}
    except OSError as error:
        raise ArchiveError("archive_corrupt", "archive directory cannot be inspected") from error
    if not _BASE_ARCHIVE_FILES.issubset(names):
        raise ArchiveError("archive_incomplete", "archive is missing required artifacts")
    return names


def _read_verified_archive(archive_path: Path, *, git: GitClient) -> dict[str, Any]:
    names = _validate_archive_layout(archive_path)
    manifest = _read_manifest(
        archive_path / "manifest.json", maximum_bytes=MAX_MANIFEST_BYTES
    )
    schema_version = manifest.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version
        not in {_LEGACY_ARCHIVE_SCHEMA_VERSION, _ARCHIVE_SCHEMA_VERSION}
    ):
        raise ArchiveError("archive_corrupt", "archive manifest has an unsupported schema")

    allowed_manifest_keys = {
        "artifacts",
        "metadata",
        "restore",
        "schema_version",
        "snapshot",
    }
    if schema_version == _ARCHIVE_SCHEMA_VERSION:
        allowed_manifest_keys.update(
            {"derivative", "git_state_artifacts", "git_state_snapshot"}
        )
    if set(manifest) - allowed_manifest_keys:
        raise ArchiveError("archive_corrupt", "archive manifest has unexpected fields")
    metadata = _normalize_metadata_value(manifest.get("metadata"))
    snapshot = _normalize_snapshot_value(manifest.get("snapshot"))

    has_git_state = (
        "git_state_snapshot" in manifest or "git_state_artifacts" in manifest
    )
    if has_git_state and schema_version != _ARCHIVE_SCHEMA_VERSION:
        raise ArchiveError("archive_corrupt", "legacy archive has unsupported Git state")
    if has_git_state and {
        "git_state_snapshot",
        "git_state_artifacts",
    } - set(manifest):
        raise ArchiveError("archive_corrupt", "archive Git state is incomplete")
    git_state_snapshot: dict[str, Any] | None = None
    git_state_artifacts: dict[str, Any] | None = None
    if has_git_state:
        git_state_snapshot = _normalize_git_state_snapshot(
            manifest["git_state_snapshot"]
        )
        git_state_artifacts = _normalize_git_state_artifacts(
            manifest["git_state_artifacts"]
        )

    artifacts = manifest.get("artifacts")
    expected_artifacts = {"history.bundle", "worktree.tar"}
    if git_state_artifacts is not None:
        expected_artifacts.update(_GIT_STATE_FILES)
    if not isinstance(artifacts, dict) or set(artifacts) != expected_artifacts:
        raise ArchiveError("archive_corrupt", "archive manifest has invalid artifact records")
    for name in expected_artifacts:
        _verify_artifact_record(archive_path / name, artifacts[name])
    if git_state_artifacts is not None:
        if {
            name: artifacts[name] for name in _GIT_STATE_FILES
        } != git_state_artifacts:
            raise ArchiveError(
                "archive_corrupt", "archive Git state artifact records do not match"
            )

    expected_files = {"manifest.json", *expected_artifacts}
    if names != expected_files:
        raise ArchiveError("archive_corrupt", "archive contains unexpected artifacts")
    _validate_private_file(archive_path / "manifest.json")
    _verify_worktree_tar(archive_path / "worktree.tar", snapshot)
    try:
        git.verify_bundle(
            archive_path / "history.bundle",
            expected_head=metadata["head_sha"],
        )
    except GitError as error:
        raise ArchiveError(
            "archive_corrupt", "history bundle cannot be restored independently"
        ) from error
    if git_state_snapshot is not None:
        try:
            git_state.verify_git_state(archive_path, git_state_snapshot)
        except (GitStateError, GitError, OSError) as error:
            raise ArchiveError(
                "archive_corrupt",
                "Git state artifacts cannot be independently verified or restored.",
            ) from error

    _validate_restore_instructions(manifest.get("restore"), metadata["head_sha"])
    _validate_derivative(manifest.get("derivative"), snapshot)
    return manifest


def _validate_restore_instructions(value: Any, head_sha: str) -> None:
    if not isinstance(value, dict) or value.get("head_sha") != head_sha:
        raise ArchiveError("archive_corrupt", "archive manifest lacks restore instructions")
    commands = value.get("commands")
    if not isinstance(commands, list) or not all(isinstance(item, str) for item in commands):
        raise ArchiveError("archive_corrupt", "archive restore instructions are invalid")


def _validate_derivative(value: Any, snapshot: dict[str, Any]) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "excluded_paths",
        "source_manifest_sha256",
        "source_snapshot_fingerprint",
    }:
        raise ArchiveError("archive_corrupt", "archive derivative metadata is invalid")
    excluded_paths = value["excluded_paths"]
    if (
        not isinstance(excluded_paths, list)
        or excluded_paths != snapshot.get("excluded_paths")
        or not _is_sha256(value["source_manifest_sha256"])
        or not _is_sha256(value["source_snapshot_fingerprint"])
    ):
        raise ArchiveError("archive_corrupt", "archive derivative metadata is invalid")


def _fsync_archive(archive_path: Path, manifest: dict[str, Any]) -> None:
    for name in {"manifest.json", *manifest["artifacts"]}:
        _fsync_private_file(archive_path / name)
    _fsync_directory(archive_path)
    _fsync_directory(archive_path.parent)


def _validate_private_file(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as error:
        raise ArchiveError("archive_incomplete", "archive artifact is missing") from error
    except OSError as error:
        raise ArchiveError("archive_corrupt", "archive artifact is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
    ):
        raise ArchiveError("archive_unsafe", "archive artifact is not private")


def _artifact_record(path: Path) -> dict[str, Any]:
    _validate_private_file(path)
    digest, size = _file_digest(path)
    return {"sha256": digest, "bytes": size}


def _validate_artifact_record(record: Any) -> None:
    if (
        not isinstance(record, dict)
        or set(record) != {"bytes", "sha256"}
        or not _is_nonnegative_int(record["bytes"])
        or not _is_sha256(record["sha256"])
    ):
        raise ArchiveError("archive_corrupt", "archive artifact record is invalid")


def _verify_artifact_record(path: Path, record: Any) -> None:
    _validate_private_file(path)
    _validate_artifact_record(record)
    digest, size = _file_digest(path)
    if size != record["bytes"] or digest != record["sha256"]:
        raise ArchiveError("archive_corrupt", "archive artifact checksum is invalid")


def _file_digest(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArchiveError("archive_corrupt", "archive artifact cannot be read") from error
    try:
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(descriptor, _CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
    except OSError as error:
        raise ArchiveError("archive_corrupt", "archive artifact cannot be read") from error
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size

def _manifest_byte_limit(
    metadata: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    require_fit: bool = False,
) -> int:
    expected = _canonical_json_bytes(
        {"metadata": metadata, "snapshot": snapshot}, "archive_corrupt"
    )
    limit = len(expected) + _MANIFEST_FIXED_OVERHEAD
    if require_fit and limit > MAX_MANIFEST_BYTES:
        raise ArchiveError("archive_manifest_too_large", "archive manifest exceeds size limit")
    return min(limit, MAX_MANIFEST_BYTES)


def _restore_instructions(
    head_sha: str, *, portable_restore: bool = False
) -> dict[str, Any]:
    if portable_restore:
        return {
            "head_sha": head_sha,
            "commands": [
                (
                    'awf wt archive-restore --archive "$PWD" '
                    '--destination "${PWD}-restored" --apply --json'
                )
            ],
            "notes": (
                "Run this command from the archive directory. The destination is a "
                "new sibling path beneath the private archive parent and must not "
                "already exist. This archive includes preserved Git state or was "
                "repacked; manual clone/reset/tar commands cannot restore staged "
                "changes, unmerged index stages, or index flags."
            ),
        }
    return {
        "head_sha": head_sha,
        "commands": [
            "git clone --no-checkout history.bundle restored-worktree",
            f"git -C restored-worktree reset --mixed {head_sha}",
            "tar -xpf worktree.tar -C restored-worktree",
        ],
        "notes": (
            "Run the commands from this archive directory. The bundle restores Git "
            "history independently before the tar restores the archived worktree "
            "contents, executable modes, and symlinks. Reset restores the index "
            "without recreating tracked files that were absent from the snapshot."
        ),
    }


def _fsync_private_file(path: Path) -> None:
    _validate_private_file(path)
    try:
        before = path.lstat()
    except OSError as error:
        raise ArchiveError("archive_corrupt", "archive artifact is unavailable") from error
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArchiveError("archive_corrupt", "archive artifact cannot be opened") from error
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise ArchiveError("archive_unsafe", "archive artifact changed unexpectedly")
        os.fsync(descriptor)
    except ArchiveError:
        raise
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to sync archive artifact") from error
    finally:
        os.close(descriptor)
    _validate_private_file(path)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to open archive directory") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to sync archive directory") from error
    finally:
        os.close(descriptor)


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ArchiveError("archive_destination_invalid", "unable to inspect archive destination") from error


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _canonical_json_bytes(value: Any, code: str) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ArchiveError(code, "archive data is not valid UTF-8 JSON") from error


def _json_copy(value: Any, code: str) -> Any:
    try:
        return json.loads(_canonical_json_bytes(value, code).decode("utf-8"))
    except json.JSONDecodeError as error:
        raise ArchiveError(code, "archive data is not valid JSON") from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_object_id(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_utf8(value: str, label: str) -> None:
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ArchiveError(
            "unsupported_filesystem", f"{label} cannot be represented safely in archive metadata"
        ) from error


class _ReadableByteStream(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _HashingReader:
    def __init__(self, source: _ReadableByteStream) -> None:
        self._source = source
        self._digest = hashlib.sha256()
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        value = self._source.read(size)
        if value:
            self._digest.update(value)
            self.count += len(value)
        return value

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class _HashingWriter(io.RawIOBase):
    def __init__(self, destination: BinaryIO) -> None:
        super().__init__()
        self._destination = destination
        self._digest = hashlib.sha256()
        self.count = 0

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        raise io.UnsupportedOperation("read")

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        raise io.UnsupportedOperation("seek")

    def tell(self) -> int:
        return self._destination.tell()

    def write(self, value: Any) -> int:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("archive writer requires bytes")
        written = self._destination.write(value)
        if written != len(value):
            raise OSError("short archive write")
        self._digest.update(value)
        self.count += written
        return written

    def flush(self) -> None:
        self._destination.flush()

    def fileno(self) -> int:
        return self._destination.fileno()

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _archive_source_path(path: Path) -> Path:
    supplied = Path(path)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise ArchiveError(
            "archive_source_invalid", "archive source must be an absolute direct child"
        )
    if supplied.name in {"", ".", ".."}:
        raise ArchiveError("archive_source_invalid", "archive source name is invalid")
    parent = validate_backup_root(supplied.parent, forbidden_roots=())
    return parent / supplied.name


def validate_restore_destination(destination: Path) -> Path:
    supplied = Path(destination)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise ArchiveError(
            "restore_destination_invalid",
            "restore destination must be an absolute direct child",
        )
    if supplied.name in {"", ".", ".."}:
        raise ArchiveError("restore_destination_invalid", "restore destination name is invalid")
    parent = validate_backup_root(supplied.parent, forbidden_roots=())
    restored = parent / supplied.name
    if _lstat_optional(restored) is not None:
        raise ArchiveError(
            "restore_destination_exists", "restore destination already exists"
        )
    return restored


def _restore_history_bundle(bundle: Path, destination: Path, head_sha: str) -> None:
    _validate_private_file(bundle)
    with TemporaryDirectory(prefix="awf-archive-git-template-") as temporary:
        template = Path(temporary) / "template"
        template.mkdir(mode=0o700)
        command_prefix = (
            "-c",
            f"core.hooksPath={template}",
            "-c",
            "protocol.file.allow=always",
        )
        _run_restore_git(
            *command_prefix,
            "init",
            "--quiet",
            f"--template={template}",
            str(destination),
            cwd=destination.parent,
            template=template,
        )
        _run_restore_git(
            *command_prefix,
            "fetch",
            "--no-tags",
            str(bundle.resolve()),
            "HEAD:refs/heads/archive-restore",
            cwd=destination,
            template=template,
        )
        _run_restore_git(
            *command_prefix,
            "reset",
            "--mixed",
            head_sha,
            cwd=destination,
            template=template,
        )


def _neutral_git_environment(*, template: Path | None = None) -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if template is not None:
        environment["GIT_TEMPLATE_DIR"] = str(template)
    return environment


def _run_archived_git(
    *arguments: str,
    cwd: Path,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        if input_bytes is None:
            return subprocess.run(
                ("git", *arguments),
                cwd=cwd,
                env=_neutral_git_environment(),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        return subprocess.run(
            ("git", *arguments),
            cwd=cwd,
            env=_neutral_git_environment(),
            check=False,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise GitError("unable to run Git for archive exclusion verification") from error


def _archived_tracked_paths(repository: Path, path: str) -> tuple[str, ...]:
    completed = _run_archived_git(
        "-c",
        f"core.excludesFile={os.devnull}",
        "ls-files",
        "-z",
        cwd=repository,
    )
    if completed.returncode != 0:
        raise GitError(
            "Git archive exclusion verification command failed",
            returncode=completed.returncode,
        )
    target_parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    return tuple(
        sorted(
            candidate
            for candidate in (
                os.fsdecode(record)
                for record in completed.stdout.split(b"\0")
                if record
            )
            if len(PurePosixPath(candidate).parts) >= len(target_parts)
            and tuple(
                part.casefold()
                for part in PurePosixPath(candidate).parts[: len(target_parts)]
            )
            == target_parts
        )
    )


def _archived_path_is_ignored(repository: Path, path: str) -> bool:
    completed = _run_archived_git(
        "-c",
        f"core.excludesFile={os.devnull}",
        "check-ignore",
        "--stdin",
        "-z",
        cwd=repository,
        input_bytes=os.fsencode(path) + b"\0",
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise GitError(
        "Git archive exclusion verification command failed",
        returncode=completed.returncode,
    )


def _run_restore_git(
    *arguments: str, cwd: Path, template: Path
) -> None:
    environment = _neutral_git_environment(template=template)
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=cwd,
            env=environment,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise GitError("unable to run Git for archive restoration") from error
    if completed.returncode != 0:
        raise GitError("Git archive restoration command failed", returncode=completed.returncode)


def _restore_worktree_tar(
    path: Path,
    destination: Path,
    snapshot: dict[str, Any],
    *,
    excluded_paths: tuple[str, ...],
    source_snapshot: dict[str, Any] | None = None,
) -> list[tuple[Path, int]]:
    _validate_private_file(path)
    target_entries = {entry["name"]: entry for entry in snapshot["entries"]}
    full_entries = {
        entry["name"]: entry
        for entry in (source_snapshot or snapshot)["entries"]
    }
    seen: set[str] = set()
    directory_modes: list[tuple[Path, int]] = []
    try:
        with tarfile.open(path, mode="r:") as archive:
            while True:
                member = archive.next()
                if member is None:
                    break
                source_entry = full_entries.get(member.name)
                if member.name in seen or source_entry is None:
                    raise ArchiveError(
                        "archive_corrupt", "worktree archive has unexpected entries"
                    )
                seen.add(member.name)
                _assert_tar_member_shape(member, source_entry)
                target_entry = target_entries.get(member.name)
                if target_entry is None:
                    if not _is_excluded_entry(member.name, excluded_paths):
                        raise ArchiveError(
                            "archive_corrupt", "worktree archive has unexpected entries"
                        )
                    continue
                _restore_tar_member(
                    archive, member, target_entry, destination, directory_modes
                )
    except ArchiveError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError("archive_corrupt", "worktree archive cannot be restored") from error
    if seen != set(full_entries) or set(target_entries) - seen:
        raise ArchiveError("archive_corrupt", "worktree archive is incomplete")
    return directory_modes


def _restore_directory_modes(directory_modes: list[tuple[Path, int]]) -> None:
    descriptors: list[int] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for target, mode in reversed(directory_modes):
            descriptor = os.open(target, flags)
            descriptors.append(descriptor)
            os.chmod(target, mode)
        for descriptor in descriptors:
            os.fsync(descriptor)
    except OSError as error:
        raise ArchiveError(
            "archive_restore_failed",
            "unable to restore worktree directory mode",
        ) from error
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _restore_tar_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    expected: dict[str, Any],
    destination: Path,
    directory_modes: list[tuple[Path, int]],
) -> None:
    target = _source_path(destination, expected["name"])
    if expected["type"] == "directory":
        if expected["name"] == ".":
            try:
                details = target.lstat()
            except OSError as error:
                raise ArchiveError("archive_restore_failed", "restore root is unavailable") from error
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise ArchiveError("archive_restore_failed", "restore root is unsafe")
        else:
            try:
                target.mkdir(mode=0o700)
                os.chmod(target, 0o700)
            except OSError as error:
                raise ArchiveError(
                    "archive_restore_failed", "unable to restore worktree directory"
                ) from error
        directory_modes.append((target, expected["mode"]))
        return
    if expected["type"] == "symlink":
        try:
            os.symlink(expected["link_target"], target)
        except OSError as error:
            raise ArchiveError(
                "archive_restore_failed", "unable to restore worktree symlink"
            ) from error
        return

    try:
        source = archive.extractfile(member)
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError("archive_corrupt", "worktree archive file cannot be read") from error
    if source is None:
        raise ArchiveError("archive_corrupt", "worktree archive file is unavailable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags, expected["mode"])
        os.fchmod(descriptor, expected["mode"])
        digest = hashlib.sha256()
        size = 0
        with source, os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            while True:
                block = source.read(_CHUNK_SIZE)
                if not block:
                    break
                written = output.write(block)
                if written != len(block):
                    raise OSError("short archive restore write")
                digest.update(block)
                size += len(block)
            output.flush()
            os.fsync(output.fileno())
        if size != expected["size"] or digest.hexdigest() != expected["hash"]:
            raise ArchiveError("archive_corrupt", "worktree archive file checksum is invalid")
    except ArchiveError:
        raise
    except OSError as error:
        source.close()
        raise ArchiveError("archive_restore_failed", "unable to restore worktree file") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_tar_member_shape(member: tarfile.TarInfo, expected: dict[str, Any]) -> None:
    if member.mode & 0o7777 != expected["mode"]:
        raise ArchiveError("archive_corrupt", "worktree archive mode does not match snapshot")
    entry_type = expected["type"]
    if entry_type == "directory":
        if not member.isdir() or member.size != 0:
            raise ArchiveError("archive_corrupt", "worktree archive directory is invalid")
        return
    if entry_type == "symlink":
        if not member.issym() or member.linkname != expected["link_target"]:
            raise ArchiveError("archive_corrupt", "worktree archive symlink is invalid")
        return
    if not member.isreg() or member.size != expected["size"]:
        raise ArchiveError("archive_corrupt", "worktree archive file is invalid")


def _is_excluded_entry(name: str, excluded_paths: tuple[str, ...]) -> bool:
    return any(name == path or name.startswith(f"{path}/") for path in excluded_paths)


def _validate_archived_excluded_paths(
    archive_path: Path, manifest: dict[str, Any], excluded_paths: tuple[str, ...]
) -> None:
    snapshot = _normalize_snapshot_value(manifest["snapshot"])
    entries = {entry["name"]: entry for entry in snapshot["entries"]}
    for relative_path in excluded_paths:
        entry = entries.get(relative_path)
        if entry is None or entry["type"] != "directory":
            raise ArchiveError(
                "excluded_path_unverified",
                "archive excluded path is not a directory",
            )
        ancestor = ""
        for component in PurePosixPath(relative_path).parts:
            ancestor = component if not ancestor else f"{ancestor}/{component}"
            ancestor_entry = entries.get(ancestor)
            if ancestor_entry is None or ancestor_entry["type"] != "directory":
                raise ArchiveError(
                    "excluded_path_unsafe",
                    "archive excluded path has a non-directory ancestor",
                )

    with TemporaryDirectory(prefix="awf-archive-ignore-") as temporary:
        repository = Path(temporary) / "repository"
        repository.mkdir(mode=0o700)
        metadata = _normalize_metadata_value(manifest["metadata"])
        try:
            _restore_history_bundle(
                archive_path / "history.bundle", repository, metadata["head_sha"]
            )
            git_state_snapshot = manifest.get("git_state_snapshot")
            if git_state_snapshot is not None:
                git_state.restore_git_state(archive_path, repository, git_state_snapshot)
            _restore_root_ignore_file(
                archive_path / "worktree.tar", repository, snapshot
            )
            for relative_path in excluded_paths:
                candidate = repository.joinpath(*relative_path.split("/"))
                candidate.mkdir(mode=0o700)
                if _archived_tracked_paths(repository, relative_path):
                    raise ArchiveError(
                        "excluded_path_unverified",
                        "archive excluded path contains Git-tracked descendants",
                    )
                if not _archived_path_is_ignored(repository, relative_path):
                    raise ArchiveError(
                        "excluded_path_unverified",
                        "archive excluded path is not Git-ignored",
                    )
        except ArchiveError:
            raise
        except (GitError, OSError, RuntimeError, tarfile.TarError) as error:
            raise ArchiveError(
                "excluded_path_unverified",
                "unable to verify archived ignored excluded path",
            ) from error


def _restore_root_ignore_file(
    archive_path: Path, destination: Path, snapshot: dict[str, Any]
) -> None:
    expected = next(
        (entry for entry in snapshot["entries"] if entry["name"] == ".gitignore"),
        None,
    )
    if expected is None:
        return
    _validate_private_file(archive_path)
    directory_modes: list[tuple[Path, int]] = []
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            while True:
                member = archive.next()
                if member is None:
                    break
                if member.name != ".gitignore":
                    continue
                _assert_tar_member_shape(member, expected)
                _restore_tar_member(
                    archive, member, expected, destination, directory_modes
                )
                _restore_directory_modes(directory_modes)
                return
    except ArchiveError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError(
            "excluded_path_unverified", "unable to inspect archive ignore rules"
        ) from error
    raise ArchiveError(
        "archive_corrupt", "worktree archive is missing its root ignore rules"
    )


def _filtered_snapshot(
    source_snapshot: dict[str, Any], excluded_paths: tuple[str, ...]
) -> dict[str, Any]:
    normalized_source = _normalize_snapshot_value(source_snapshot)
    entries = [
        entry
        for entry in normalized_source["entries"]
        if not _is_excluded_entry(entry["name"], excluded_paths)
    ]
    total_bytes = sum(
        entry["size"] for entry in entries if entry["type"] == "regular"
    )
    details: dict[str, Any] = {
        "entries": entries,
        "excluded_paths": list(excluded_paths),
        "total_bytes": total_bytes,
        "root": normalized_source["root"],
    }
    return {
        "fingerprint": _sha256_bytes(
            _canonical_json_bytes(details, "snapshot_invalid")
        ),
        **details,
    }


def _copy_private_artifact(source: Path, destination: Path) -> dict[str, Any]:
    _validate_private_file(source)
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    try:
        source_descriptor = os.open(
            source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_descriptor = _open_private_output(destination)
        with os.fdopen(source_descriptor, "rb", closefd=True) as input_file:
            source_descriptor = None
            with os.fdopen(destination_descriptor, "wb", closefd=True) as output:
                destination_descriptor = None
                with _HashingWriter(output) as writer:
                    while True:
                        block = input_file.read(_CHUNK_SIZE)
                        if not block:
                            break
                        writer.write(block)
                    writer.flush()
                    os.fsync(output.fileno())
    except OSError as error:
        raise ArchiveError("archive_write_failed", "unable to copy archive artifact") from error
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
    _seal_private_file(destination)
    return {"sha256": writer.hexdigest, "bytes": writer.count}


def _write_filtered_worktree_tar(
    source_path: Path,
    destination_path: Path,
    source_snapshot: dict[str, Any],
    filtered_snapshot: dict[str, Any],
) -> dict[str, Any]:
    _validate_private_file(source_path)
    source_entries = {
        entry["name"]: entry for entry in source_snapshot["entries"]
    }
    filtered_entries = {
        entry["name"]: entry for entry in filtered_snapshot["entries"]
    }
    descriptor: int | None = None
    try:
        descriptor = _open_private_output(destination_path)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            with _HashingWriter(output) as writer:
                with tarfile.open(source_path, mode="r:") as source_archive:
                    with tarfile.open(
                        fileobj=writer, mode="w", format=tarfile.PAX_FORMAT
                    ) as destination_archive:
                        seen: set[str] = set()
                        while True:
                            member = source_archive.next()
                            if member is None:
                                break
                            expected = source_entries.get(member.name)
                            if member.name in seen or expected is None:
                                raise ArchiveError(
                                    "archive_corrupt",
                                    "worktree archive has unexpected entries",
                                )
                            seen.add(member.name)
                            _assert_tar_member_shape(member, expected)
                            if member.name not in filtered_entries:
                                continue
                            _copy_tar_member(
                                source_archive,
                                destination_archive,
                                member,
                                filtered_entries[member.name],
                            )
                        if seen != set(source_entries):
                            raise ArchiveError(
                                "archive_corrupt", "worktree archive is incomplete"
                            )
                writer.flush()
                os.fsync(output.fileno())
    except ArchiveError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError(
            "archive_write_failed", "unable to write filtered worktree archive"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _seal_private_file(destination_path)
    return {"sha256": writer.hexdigest, "bytes": writer.count}


def _copy_tar_member(
    source_archive: tarfile.TarFile,
    destination_archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    expected: dict[str, Any],
) -> None:
    info = _tar_info(expected)
    if expected["type"] != "regular":
        destination_archive.addfile(info)
        return
    try:
        source = source_archive.extractfile(member)
    except (OSError, tarfile.TarError) as error:
        raise ArchiveError("archive_corrupt", "worktree archive file cannot be read") from error
    if source is None:
        raise ArchiveError("archive_corrupt", "worktree archive file is unavailable")
    with source:
        reader = _HashingReader(source)
        destination_archive.addfile(info, reader)
    if reader.count != expected["size"] or reader.hexdigest != expected["hash"]:
        raise ArchiveError("archive_corrupt", "worktree archive file checksum is invalid")


def _tar_info(entry: dict[str, Any]) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry["name"])
    info.mode = entry["mode"]
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if entry["type"] == "directory":
        info.type = tarfile.DIRTYPE
        info.size = 0
    elif entry["type"] == "symlink":
        info.type = tarfile.SYMTYPE
        info.linkname = entry["link_target"]
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.size = entry["size"]
    return info


def _restore_summary(
    archive_path: Path,
    destination: Path,
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    git_state_restored: bool,
) -> dict[str, Any]:
    return {
        "archive_path": str(archive_path),
        "destination": str(destination),
        "git_state_restored": git_state_restored,
        "schema_version": manifest["schema_version"],
        "snapshot": {
            "entry_count": len(snapshot["entries"]),
            "excluded_paths": snapshot.get("excluded_paths", []),
            "fingerprint": snapshot["fingerprint"],
            "total_bytes": snapshot["total_bytes"],
        },
    }
