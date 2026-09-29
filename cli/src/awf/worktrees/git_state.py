from __future__ import annotations

"""Self-contained preservation and safe disposal of mutable Git worktree state.

This module deliberately keeps Git's mutable working-state files separate from the
normal history bundle.  It never copies configuration, hooks, gitdir/commondir
pointers, or lock files into an archive, and every Git child process starts with a
sanitised Git environment.
"""

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .git import GitClient, GitError


_STATE_VERSION = 1
_GIT_STATE_TAR = "git-state.tar"
_GIT_OBJECT_PACK = "git-objects.pack"
_GIT_STATE_ARTIFACTS = frozenset({_GIT_STATE_TAR, _GIT_OBJECT_PACK})
_DIRECT_OPERATION_FILES = (
    "AM_HEAD",
    "AUTO_MERGE",
    "CHERRY_PICK_HEAD",
    "MERGE_HEAD",
    "MERGE_MODE",
    "MERGE_MSG",
    "ORIG_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
    "SQUASH_MSG",
)
_OPERATION_DIRECTORIES = ("rebase-apply", "rebase-merge", "sequencer")
_OBJECT_FORMATS = {"sha1": 40, "sha256": 64}
_FILE_RECORD_FIELDS = frozenset({"bytes", "sha256"})
_OPTIONAL_FILE_RECORD_FIELDS = frozenset({"present", "bytes", "sha256"})
_OPERATION_RECORD_FIELDS = frozenset({"name", "bytes", "sha256"})
_LOCK_RECORD_FIELDS = frozenset({"bytes", "sha256", "device", "inode"})
_MAX_METADATA_BYTES = 64 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_GIT_PREFIX = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "commit.gpgSign=false",
)


class GitStateError(GitError):
    """Raised when mutable Git state cannot be safely captured or restored."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = "git_state_failed"


def snapshot_git_state(git: GitClient, worktree_path: Path) -> dict[str, Any]:
    """Return an opaque, fingerprinted record of restorable mutable Git state.

    The raw index itself is deliberately not exposed in the record.  Its exact
    byte digest binds cache extensions, conflict stages, skip-worktree and
    assume-unchanged flags to the archive while the raw bytes live in the tar.
    """

    worktree = _require_worktree(worktree_path)
    environment = _neutral_environment()
    git_dir, index_path, _object_directory = _git_paths(git, worktree, environment)
    object_format = _git_text(
        git, "rev-parse", "--show-object-format", cwd=worktree, env=environment
    )
    oid_length = _OBJECT_FORMATS.get(object_format)
    if oid_length is None:
        raise GitStateError("Git uses an unsupported object format")

    head_raw = _read_regular_file(git_dir / "HEAD", label="Git HEAD")
    head = _parse_head(git, worktree, head_raw, oid_length, environment)
    index = _file_record(index_path, required=False, label="Git index")
    index_objects = _index_object_ids(git, worktree, oid_length, environment)
    metadata = _operation_metadata(git_dir)
    operation_objects = _metadata_object_ids(
        git, worktree, metadata, oid_length, environment
    )

    details: dict[str, Any] = {
        "version": _STATE_VERSION,
        "object_format": object_format,
        "head": head,
        "index": index,
        "index_objects": index_objects,
        "operations": [record for record, _path in metadata],
        "operation_objects": operation_objects,
    }
    return {"fingerprint": _sha256(_canonical_json(details)), **details}


def write_git_state(
    git: GitClient,
    worktree_path: Path,
    destination: Path,
    expected: dict[str, Any],
) -> dict[str, dict[str, int | str]]:
    """Write and independently validate the state tar and object closure pack."""

    normalized = _normalise_snapshot(expected)
    worktree = _require_worktree(worktree_path)
    archive_path = _require_private_directory(destination, label="archive destination")
    if snapshot_git_state(git, worktree) != normalized:
        raise GitStateError("Git state changed before archival began")

    tar_path = archive_path / _GIT_STATE_TAR
    pack_path = archive_path / _GIT_OBJECT_PACK
    if tar_path.exists() or pack_path.exists():
        raise GitStateError("Git state artifact already exists")

    try:
        _write_state_tar(git, worktree, tar_path, normalized)
        _write_object_pack(git, worktree, pack_path, normalized)
        if snapshot_git_state(git, worktree) != normalized:
            raise GitStateError("Git state changed while archival was written")
        artifacts = {
            _GIT_STATE_TAR: _private_artifact_record(tar_path),
            _GIT_OBJECT_PACK: _private_artifact_record(pack_path),
        }
        verify_git_state(archive_path, normalized)
        _fsync_directory(archive_path)
        return artifacts
    except BaseException:
        # A partial archive must never look complete.  The archive-format owner
        # keeps the containing directory private and will reject an incomplete set.
        for path in (tar_path, pack_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def verify_git_state(archive_path: Path, expected: dict[str, Any]) -> None:
    """Verify tar shape and object closure without using the source repository."""

    normalized = _normalise_snapshot(expected)
    archive = _require_private_directory(archive_path, label="archive destination")
    tar_path = archive / _GIT_STATE_TAR
    pack_path = archive / _GIT_OBJECT_PACK
    _validate_private_file(tar_path, label="Git state tar")
    _validate_private_file(pack_path, label="Git object pack")
    index_data, metadata = _read_state_tar(tar_path, normalized)

    with TemporaryDirectory(prefix="awf-git-state-verify-") as temporary:
        root = Path(temporary)
        template = root / "template"
        isolated = root / "isolated.git"
        template.mkdir(mode=0o700)
        environment = _neutral_environment()
        _private_git(
            "init",
            "--bare",
            f"--template={template}",
            str(isolated),
            cwd=root,
            env=environment,
        )
        objects = isolated / "objects"
        copied_pack = objects / "pack" / "pack-awf-git-state.pack"
        _copy_private_file(pack_path, copied_pack)
        _private_git("index-pack", "--strict", str(copied_pack), cwd=isolated, env=environment)
        _private_git(
            "--git-dir",
            str(isolated),
            "fsck",
            "--full",
            "--strict",
            "--no-reflogs",
            cwd=root,
            env=environment,
        )
        _verify_object_ids(
            normalized,
            cwd=root,
            git_dir=isolated,
            environment=environment,
        )
        if index_data is not None:
            temporary_index = root / "index"
            _write_new_file(temporary_index, index_data)
            index_environment = dict(environment)
            index_environment["GIT_INDEX_FILE"] = str(temporary_index)
            output = _private_git(
                "--git-dir",
                str(isolated),
                "ls-files",
                "--stage",
                "-z",
                cwd=root,
                env=index_environment,
            )
            ids = _parse_index_ids(output, _oid_length(normalized))
            if ids != normalized["index_objects"]:
                raise GitStateError("Git index object closure does not match its snapshot")
        # Reading metadata through the tar already verifies every byte.  Keep the
        # variable live so accidental removal of this validation is apparent.
        if len(metadata) != len(normalized["operations"]):
            raise GitStateError("Git operation metadata is incomplete")


def restore_git_state(
    archive_path: Path,
    destination_repo: Path,
    expected: dict[str, Any],
) -> None:
    """Restore mutable state into an independent destination repository.

    No source repository, alternates file, source configuration, hooks, or source
    worktree path is consulted.  The destination must be a standalone repository
    created by the archive restore transaction, not a linked worktree.
    """

    normalized = _normalise_snapshot(expected)
    verify_git_state(archive_path, normalized)
    archive = _require_private_directory(archive_path, label="archive destination")
    index_data, metadata = _read_state_tar(archive / _GIT_STATE_TAR, normalized)
    destination = _require_worktree(destination_repo)
    environment = _neutral_environment()
    git_dir_text = _private_git(
        "rev-parse", "--path-format=absolute", "--git-dir", cwd=destination, env=environment
    ).decode("utf-8", errors="strict").strip()
    common_dir_text = _private_git(
        "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=destination, env=environment
    ).decode("utf-8", errors="strict").strip()
    git_dir = Path(git_dir_text).resolve()
    common_dir = Path(common_dir_text).resolve()
    if git_dir != common_dir:
        raise GitStateError("Git state restore requires an independent destination repository")
    _require_git_directory(git_dir, label="destination Git directory")
    objects_text = _private_git(
        "rev-parse", "--path-format=absolute", "--git-path", "objects", cwd=destination, env=environment
    ).decode("utf-8", errors="strict").strip()
    object_directory = _require_git_directory(Path(objects_text), label="destination object directory")

    _install_pack(archive / _GIT_OBJECT_PACK, object_directory, destination, environment)
    _clear_operation_metadata(git_dir)
    for name, content in metadata.items():
        target = _metadata_destination(git_dir, name)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _assert_safe_directory_chain(git_dir, target.parent)
        _write_replace_file(target, content)

    index_text = _private_git(
        "rev-parse", "--path-format=absolute", "--git-path", "index", cwd=destination, env=environment
    ).decode("utf-8", errors="strict").strip()
    _replace_index(Path(index_text), index_data)

    head = normalized["head"]
    if head["kind"] == "symbolic":
        _private_git(
            "update-ref", head["ref"], head["oid"], cwd=destination, env=environment
        )
        _private_git("symbolic-ref", "HEAD", head["ref"], cwd=destination, env=environment)
    else:
        _private_git(
            "update-ref", "--no-deref", "HEAD", head["oid"], cwd=destination, env=environment
        )

    restored = snapshot_git_state(GitClient(destination), destination)
    if restored != normalized:
        raise GitStateError("restored Git state does not match its archived snapshot")


@contextmanager
def archived_removal_checkpoint(
    git: GitClient,
    worktree_path: Path,
    *,
    snapshot: dict[str, Any],
    git_state: dict[str, Any],
    private_root: Path,
    exclude_ignored_paths: tuple[str, ...] = (),
) -> Iterator[dict[str, str]]:
    """Temporarily make an archived dirty worktree removable without ``--force``.

    Only the worktree's HEAD and index are changed, under their native Git locks.
    The synthetic commit and every blob generated from included untracked or ignored
    files live exclusively in the journal's private object directory.  If removal fails,
    HEAD and index are restored by exact content comparison; working files are
    never reset, cleaned, stashed, or otherwise written.
    """

    normalized = _normalise_snapshot(snapshot)
    artifacts = _normalise_artifacts(git_state)
    worktree = _require_worktree(worktree_path)
    from . import archive

    excluded_paths = archive._normalize_excluded_paths(exclude_ignored_paths)
    archive._validate_excluded_paths(worktree, excluded_paths)
    root = _require_private_directory(private_root, label="checkpoint root")
    if snapshot_git_state(git, worktree) != normalized:
        raise GitStateError("Git state changed before checkpoint creation")

    journal = _journal_path(root, worktree)
    if journal.exists():
        raise GitStateError("an archived removal checkpoint requires recovery")
    journal.parent.mkdir(mode=0o700, exist_ok=True)
    _require_private_directory(journal.parent, label="checkpoint journal root")
    try:
        journal.mkdir(mode=0o700)
    except OSError as error:
        raise GitStateError("unable to create private checkpoint journal") from error
    _fsync_directory(journal.parent)

    checkpoint_attempted = False
    try:
        environment = _neutral_environment()
        git_dir, index_path, common_objects = _git_paths(git, worktree, environment)
        checkpoint_objects = journal / "objects"
        checkpoint_objects.mkdir(mode=0o700)
        (checkpoint_objects / "pack").mkdir(mode=0o700)
        checkpoint_index = journal / "checkpoint-index"
        checkpoint_environment = _checkpoint_environment(
            checkpoint_objects, common_objects, index_path=checkpoint_index
        )
        checkpoint_head = _build_checkpoint_commit(
            git,
            worktree,
            checkpoint_environment,
            checkpoint_index,
            _oid_length(normalized),
            excluded_paths,
        )
        _seal_private_file(checkpoint_index, label="checkpoint index")
        _fsync_private_tree(checkpoint_objects)
        checkpoint_index_record = _file_record(
            checkpoint_index, required=True, label="checkpoint index"
        )
        if snapshot_git_state(git, worktree) != normalized:
            raise GitStateError("Git state changed while checkpoint was prepared")

        original_head = _read_regular_file(git_dir / "HEAD", label="Git HEAD")
        original_index = _read_optional_regular_file(index_path, label="Git index")
        checkpoint_head_bytes = checkpoint_head.encode("ascii") + b"\n"
        checkpoint_head_source = journal / "checkpoint-head"
        _write_new_file(journal / "original-head", original_head)
        _write_new_file(checkpoint_head_source, checkpoint_head_bytes)
        if original_index is not None:
            rollback_index_source = journal / "original-index"
            _write_new_file(rollback_index_source, original_index)
        else:
            rollback_index_source = journal / "rollback-index"
            _write_new_file(rollback_index_source, b"")
        lock_sources = {
            "checkpoint_head": checkpoint_head_source,
            "checkpoint_index": checkpoint_index,
            "rollback_head": journal / "original-head",
            "rollback_index": rollback_index_source,
        }
        locks = {
            name: _lock_record(path, label=f"checkpoint {name} lock")
            for name, path in lock_sources.items()
        }
        journal_value = {
            "version": 2,
            "worktree": str(worktree),
            "git_dir": str(git_dir),
            "index_path": str(index_path),
            "git_state": artifacts,
            "original_head": _bytes_record(original_head),
            "original_index": (
                _bytes_record(original_index) if original_index is not None else None
            ),
            "checkpoint_head": checkpoint_head,
            "checkpoint_index": checkpoint_index_record,
            "locks": locks,
        }
        _write_new_file(journal / "journal.json", _canonical_json(journal_value) + b"\n")
        _fsync_directory(journal)
        _fsync_directory(journal.parent)

        checkpoint_attempted = True
        _apply_checkpoint(
            git_dir / "HEAD",
            index_path,
            original_head=original_head,
            original_index=original_index,
            checkpoint_head_source=checkpoint_head_source,
            checkpoint_index_source=checkpoint_index,
            locks=locks,
        )
        yield _checkpoint_environment(checkpoint_objects, common_objects)
    except BaseException:
        if checkpoint_attempted:
            if not recover_archived_removal_checkpoint(
                git, worktree, git_state=artifacts, private_root=root
            ):
                raise GitStateError(
                    "checkpoint rollback could not prove the original HEAD and index"
                )
        else:
            _remove_private_tree(journal)
        raise
    else:
        if not recover_archived_removal_checkpoint(
            git, worktree, git_state=artifacts, private_root=root
        ):
            raise GitStateError("checkpoint journal disappeared before it could be finalized")


def recover_archived_removal_checkpoint(
    git: GitClient,
    worktree_path: Path,
    *,
    git_state: dict[str, Any],
    private_root: Path,
) -> bool:
    """Recover a journaled checkpoint or clean one whose worktree was removed.

    ``False`` means no journal exists.  Any journal that is malformed, belongs to
    another source, or observes HEAD/index content outside its two recorded states
    is unsafe and raises an ArchiveError without deleting its backing objects.
    """

    try:
        artifacts = _normalise_artifacts(git_state)
        supplied = Path(worktree_path)
        try:
            supplied.lstat()
        except FileNotFoundError:
            worktree_missing = True
        except OSError as error:
            raise GitStateError("worktree recovery path is unavailable") from error
        else:
            worktree_missing = False
        try:
            canonical = supplied.resolve(strict=False)
        except OSError as error:
            raise GitStateError("worktree recovery path is invalid") from error
        root = _require_private_directory(private_root, label="checkpoint root")
        journal = _journal_path(root, canonical)
        try:
            journal.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise GitStateError("checkpoint journal is unavailable") from error
        journal_value = _read_journal(journal)
        if (
            journal_value["git_state"] != artifacts
            or journal_value["worktree"] != str(canonical)
        ):
            raise GitStateError("checkpoint journal does not match the verified archive")
        if worktree_missing:
            _remove_private_tree(journal)
            return True

        worktree = _require_worktree(supplied)
        if worktree != canonical:
            raise GitStateError("worktree recovery path changed unexpectedly")
        environment = _neutral_environment()
        git_dir, index_path, _common_objects = _git_paths(git, worktree, environment)
        if (
            journal_value["git_dir"] != str(git_dir)
            or journal_value["index_path"] != str(index_path)
        ):
            raise GitStateError("checkpoint journal does not match this Git worktree")
        lock_sources = _journal_lock_sources(journal)
        locks = journal_value["locks"]
        _validate_journal_locks(lock_sources, locks)
        head_lock = git_dir / "HEAD.lock"
        index_lock = index_path.with_name(index_path.name + ".lock")
        for source_name in ("checkpoint_head", "rollback_head"):
            _reclaim_journal_lock(head_lock, lock_sources[source_name], locks[source_name])
        for source_name in ("checkpoint_index", "rollback_index"):
            _reclaim_journal_lock(index_lock, lock_sources[source_name], locks[source_name])
        original_head = _read_regular_file(journal / "original-head", label="checkpoint HEAD backup")
        if _bytes_record(original_head) != journal_value["original_head"]:
            raise GitStateError("checkpoint HEAD backup is invalid")
        original_index_record = journal_value["original_index"]
        original_index = (
            _read_regular_file(journal / "original-index", label="checkpoint index backup")
            if original_index_record is not None
            else None
        )
        if original_index is not None and _bytes_record(original_index) != original_index_record:
            raise GitStateError("checkpoint index backup is invalid")
        checkpoint_index = journal / "checkpoint-index"
        if _file_record(checkpoint_index, required=True, label="checkpoint index") != journal_value["checkpoint_index"]:
            raise GitStateError("checkpoint index is invalid")
        checkpoint_head = journal_value["checkpoint_head"].encode("ascii") + b"\n"
        if not _is_object_id(
            journal_value["checkpoint_head"],
            _oid_length_from_head(original_head, git, worktree, environment),
        ):
            raise GitStateError("checkpoint HEAD is invalid")
        _restore_checkpoint_files(
            git_dir / "HEAD",
            index_path,
            original_head=original_head,
            original_index=original_index,
            checkpoint_head=checkpoint_head,
            checkpoint_index=checkpoint_index,
            rollback_head_source=lock_sources["rollback_head"],
            rollback_index_source=lock_sources["rollback_index"],
            rollback_head_record=locks["rollback_head"],
            rollback_index_record=locks["rollback_index"],
        )
        _remove_private_tree(journal)
        return True
    except GitStateError as error:
        raise _checkpoint_archive_error(error) from error


def _checkpoint_archive_error(error: GitStateError) -> RuntimeError:
    # archive.py imports this module, so ArchiveError is intentionally local.
    from .archive import ArchiveError

    return ArchiveError(
        "archive_checkpoint_unsafe",
        "The archived-removal checkpoint is unsafe; its recovery evidence was retained.",
    )


# -- archive representation -------------------------------------------------


def _normalise_snapshot(value: Any) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise GitStateError("Git state snapshot is not JSON-safe") from error
    required = {
        "fingerprint",
        "version",
        "object_format",
        "head",
        "index",
        "index_objects",
        "operations",
        "operation_objects",
    }
    if not isinstance(copied, dict) or set(copied) != required:
        raise GitStateError("Git state snapshot has an invalid shape")
    if copied["version"] != _STATE_VERSION:
        raise GitStateError("Git state snapshot has an unsupported version")
    length = _OBJECT_FORMATS.get(copied["object_format"])
    if length is None:
        raise GitStateError("Git state snapshot has an invalid object format")
    _normalise_head(copied["head"], length)
    _normalise_file_record(copied["index"], allow_absent=True)
    copied["index_objects"] = _normalise_object_ids(copied["index_objects"], length)
    copied["operation_objects"] = _normalise_object_ids(copied["operation_objects"], length)
    if not isinstance(copied["operations"], list):
        raise GitStateError("Git operation metadata has an invalid shape")
    prior = ""
    for record in copied["operations"]:
        if not isinstance(record, dict) or set(record) != _OPERATION_RECORD_FIELDS:
            raise GitStateError("Git operation metadata has an invalid record")
        name = record["name"]
        if not isinstance(name, str) or not _safe_metadata_name(name) or name <= prior:
            raise GitStateError("Git operation metadata has an invalid name")
        prior = name
        _normalise_file_record(record, expected_fields=_OPERATION_RECORD_FIELDS)
    details = {key: copied[key] for key in copied if key != "fingerprint"}
    if not _is_sha256(copied["fingerprint"]) or copied["fingerprint"] != _sha256(_canonical_json(details)):
        raise GitStateError("Git state snapshot fingerprint is invalid")
    return copied


def _normalise_head(value: Any, oid_length: int) -> None:
    if not isinstance(value, dict):
        raise GitStateError("Git HEAD snapshot has an invalid shape")
    if value.get("kind") == "symbolic":
        if set(value) != {"kind", "ref", "oid"} or not _safe_branch_ref(value["ref"]):
            raise GitStateError("Git symbolic HEAD snapshot is invalid")
    elif value.get("kind") == "detached":
        if set(value) != {"kind", "oid"}:
            raise GitStateError("Git detached HEAD snapshot is invalid")
    else:
        raise GitStateError("Git HEAD snapshot is invalid")
    if not _is_object_id(value["oid"], oid_length):
        raise GitStateError("Git HEAD snapshot has an invalid object")


def _normalise_file_record(
    value: Any,
    *,
    allow_absent: bool = False,
    expected_fields: frozenset[str] = _FILE_RECORD_FIELDS,
) -> None:
    if allow_absent and value == {"present": False}:
        return
    expected = _OPTIONAL_FILE_RECORD_FIELDS if allow_absent else expected_fields
    if not isinstance(value, dict) or set(value) != expected:
        raise GitStateError("Git state file record is invalid")
    if allow_absent and value["present"] is not True:
        raise GitStateError("Git state file presence is invalid")
    if not isinstance(value["bytes"], int) or isinstance(value["bytes"], bool) or value["bytes"] < 0:
        raise GitStateError("Git state file size is invalid")
    if not _is_sha256(value["sha256"]):
        raise GitStateError("Git state file checksum is invalid")


def _normalise_object_ids(value: Any, length: int) -> list[str]:
    if not isinstance(value, list) or value != sorted(set(value)):
        raise GitStateError("Git state object ids are invalid")
    if not all(isinstance(item, str) and _is_object_id(item, length) for item in value):
        raise GitStateError("Git state object id is invalid")
    return value


def _normalise_artifacts(value: Any) -> dict[str, dict[str, int | str]]:
    if not isinstance(value, dict) or set(value) != _GIT_STATE_ARTIFACTS:
        raise GitStateError("Git state artifact records are invalid")
    result: dict[str, dict[str, int | str]] = {}
    for name in sorted(_GIT_STATE_ARTIFACTS):
        record = value[name]
        if not isinstance(record, dict) or set(record) != {"bytes", "sha256"}:
            raise GitStateError("Git state artifact record is invalid")
        size = record["bytes"]
        digest = record["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not _is_sha256(digest):
            raise GitStateError("Git state artifact record is invalid")
        result[name] = {"bytes": size, "sha256": digest}
    return result


def _write_state_tar(
    git: GitClient, worktree: Path, target: Path, expected: dict[str, Any]
) -> None:
    environment = _neutral_environment()
    git_dir, index_path, _objects = _git_paths(git, worktree, environment)
    metadata = _operation_metadata(git_dir)
    records = [record for record, _path in metadata]
    if records != expected["operations"]:
        raise GitStateError("Git operation metadata changed before it could be written")
    index_data = (
        _read_regular_file(index_path, label="Git index") if expected["index"]["present"] else None
    )
    if index_data is not None and _bytes_record(index_data) != _without_present(expected["index"]):
        raise GitStateError("Git index changed before it could be written")

    descriptor = _open_private_output(target)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
                _add_tar_bytes(archive, "state.json", _canonical_json(expected) + b"\n")
                if index_data is not None:
                    _add_tar_bytes(archive, "index", index_data)
                for record, path in metadata:
                    data = _read_regular_file(path, label="Git operation metadata")
                    if _bytes_record(data) != {"bytes": record["bytes"], "sha256": record["sha256"]}:
                        raise GitStateError("Git operation metadata changed while it was written")
                    _add_tar_bytes(archive, f"metadata/{record['name']}", data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _validate_private_file(target, label="Git state tar")


def _write_object_pack(
    git: GitClient, worktree: Path, target: Path, expected: dict[str, Any]
) -> None:
    revisions = [
        expected["head"]["oid"],
        *expected["index_objects"],
        *expected["operation_objects"],
    ]
    payload = ("\n".join(sorted(set(revisions))) + "\n").encode("ascii")
    descriptor = _open_private_output(target)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            environment = _neutral_environment()
            try:
                process = subprocess.Popen(
                    ["git", *_GIT_PREFIX, "pack-objects", "--revs", "--stdout"],
                    cwd=str(worktree),
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    env=environment,
                )
                _stdout, _stderr = process.communicate(input=payload, timeout=git.timeout)
            except subprocess.TimeoutExpired as error:
                _stop_process(process)
                raise GitStateError("Git object closure creation timed out") from error
            except OSError as error:
                raise GitStateError("unable to create Git object closure") from error
            if process.returncode != 0:
                raise GitStateError("unable to create Git object closure")
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _validate_private_file(target, label="Git object pack")


def _read_state_tar(
    path: Path, expected: dict[str, Any]
) -> tuple[bytes | None, dict[str, bytes]]:
    expected_names = {"state.json"}
    expected_sizes: dict[str, int] = {
        "state.json": len(_canonical_json(expected)) + 1
    }
    if expected["index"]["present"]:
        expected_names.add("index")
        expected_sizes["index"] = expected["index"]["bytes"]
    for record in expected["operations"]:
        name = f"metadata/{record['name']}"
        expected_names.add(name)
        expected_sizes[name] = record["bytes"]
    observed: dict[str, bytes] = {}
    try:
        with tarfile.open(path, mode="r") as archive:
            for member in archive:
                if not member.isfile() or member.name not in expected_names or member.name in observed:
                    raise GitStateError("Git state tar has an unsafe member")
                if (
                    member.size != expected_sizes[member.name]
                    or member.mode & 0o7777 != 0o600
                    or member.uid != 0
                    or member.gid != 0
                ):
                    raise GitStateError("Git state tar member metadata is invalid")
                source = archive.extractfile(member)
                if source is None:
                    raise GitStateError("Git state tar member is unreadable")
                with source:
                    observed[member.name] = source.read()
    except (OSError, tarfile.TarError) as error:
        raise GitStateError("Git state tar cannot be read") from error
    if set(observed) != expected_names:
        raise GitStateError("Git state tar is incomplete")
    if observed["state.json"] != _canonical_json(expected) + b"\n":
        raise GitStateError("Git state tar snapshot does not match its expected state")
    index_data = observed.get("index")
    if expected["index"]["present"]:
        if index_data is None or _bytes_record(index_data) != _without_present(expected["index"]):
            raise GitStateError("Git state tar index does not match its snapshot")
    metadata: dict[str, bytes] = {}
    for record in expected["operations"]:
        name = record["name"]
        data = observed[f"metadata/{name}"]
        if _bytes_record(data) != {"bytes": record["bytes"], "sha256": record["sha256"]}:
            raise GitStateError("Git state tar operation metadata does not match its snapshot")
        metadata[name] = data
    return index_data, metadata


def _verify_object_ids(
    expected: dict[str, Any], *, cwd: Path, git_dir: Path, environment: dict[str, str]
) -> None:
    required = sorted(
        set(
            [
                expected["head"]["oid"],
                *expected["index_objects"],
                *expected["operation_objects"],
            ]
        )
    )
    for object_id in required:
        _private_git(
            "--git-dir",
            str(git_dir),
            "cat-file",
            "-e",
            f"{object_id}^{{object}}",
            cwd=cwd,
            env=environment,
        )


# -- checkpoint -------------------------------------------------------------


def _build_checkpoint_commit(
    git: GitClient,
    worktree: Path,
    environment: dict[str, str],
    index_path: Path,
    oid_length: int,
    excluded_paths: tuple[str, ...],
) -> str:
    _git(git, "read-tree", "--empty", cwd=worktree, env=environment)
    entries = bytearray()
    for relative, path, details in _walk_checkpoint_files(worktree, excluded_paths):
        if stat.S_ISREG(details.st_mode):
            object_id = _hash_regular_file(git, path, details, worktree, environment, oid_length)
            mode = "100755" if details.st_mode & stat.S_IXUSR else "100644"
        elif stat.S_ISLNK(details.st_mode):
            try:
                value = os.fsencode(os.readlink(path))
            except OSError as error:
                raise GitStateError("unable to read checkpoint symlink") from error
            object_id = _hash_bytes(git, value, worktree, environment, oid_length)
            mode = "120000"
        else:
            raise GitStateError("checkpoint worktree has an unsupported entry")
        entries.extend(mode.encode("ascii"))
        entries.extend(b" ")
        entries.extend(object_id.encode("ascii"))
        entries.extend(b"\t")
        entries.extend(os.fsencode(relative))
        entries.extend(b"\0")
    if entries:
        _git(
            git,
            "update-index",
            "--add",
            "-z",
            "--index-info",
            cwd=worktree,
            input_bytes=bytes(entries),
            env=environment,
        )
    tree = _git_text(git, "write-tree", cwd=worktree, env=environment)
    if not _is_object_id(tree, oid_length):
        raise GitStateError("Git produced an invalid checkpoint tree")
    commit_environment = dict(environment)
    commit_environment.update(
        {
            "GIT_AUTHOR_NAME": "AWF archive checkpoint",
            "GIT_AUTHOR_EMAIL": "archive-checkpoint@invalid",
            "GIT_COMMITTER_NAME": "AWF archive checkpoint",
            "GIT_COMMITTER_EMAIL": "archive-checkpoint@invalid",
        }
    )
    commit = _git_text(
        git,
        "commit-tree",
        tree,
        "-m",
        "AWF temporary archive removal checkpoint",
        cwd=worktree,
        env=commit_environment,
    )
    if not _is_object_id(commit, oid_length):
        raise GitStateError("Git produced an invalid checkpoint commit")
    if _file_record(index_path, required=True, label="checkpoint index")["bytes"] == 0:
        raise GitStateError("Git did not write a checkpoint index")
    return commit


def _apply_checkpoint(
    head_path: Path,
    index_path: Path,
    *,
    original_head: bytes,
    original_index: bytes | None,
    checkpoint_head_source: Path,
    checkpoint_index_source: Path,
    locks: dict[str, dict[str, int | str]],
) -> None:
    head_lock = head_path.with_name(head_path.name + ".lock")
    index_lock = index_path.with_name(index_path.name + ".lock")
    published: list[tuple[Path, Path, dict[str, int | str]]] = []
    try:
        _publish_journal_lock(
            checkpoint_head_source, locks["checkpoint_head"], head_lock
        )
        published.append((head_lock, checkpoint_head_source, locks["checkpoint_head"]))
        _publish_journal_lock(
            checkpoint_index_source, locks["checkpoint_index"], index_lock
        )
        published.append((index_lock, checkpoint_index_source, locks["checkpoint_index"]))
        if _read_regular_file(head_path, label="Git HEAD") != original_head:
            raise GitStateError("Git HEAD changed before checkpoint application")
        if _read_optional_regular_file(index_path, label="Git index") != original_index:
            raise GitStateError("Git index changed before checkpoint application")
        os.replace(index_lock, index_path)
        _fsync_directory(index_path.parent)
        os.replace(head_lock, head_path)
        _fsync_directory(head_path.parent)
    except BaseException:
        for lock, source, record in reversed(published):
            _reclaim_journal_lock(lock, source, record)
        raise


def _restore_checkpoint_files(
    head_path: Path,
    index_path: Path,
    *,
    original_head: bytes,
    original_index: bytes | None,
    checkpoint_head: bytes,
    checkpoint_index: Path,
    rollback_head_source: Path,
    rollback_index_source: Path,
    rollback_head_record: dict[str, int | str],
    rollback_index_record: dict[str, int | str],
) -> None:
    checkpoint_data = _read_regular_file(checkpoint_index, label="checkpoint index")
    current_head = _read_regular_file(head_path, label="Git HEAD")
    current_index = _read_optional_regular_file(index_path, label="Git index")
    if current_head not in {original_head, checkpoint_head}:
        raise GitStateError("Git HEAD changed outside the archived checkpoint")
    if current_index not in {original_index, checkpoint_data}:
        raise GitStateError("Git index changed outside the archived checkpoint")
    if current_head == original_head and current_index == original_index:
        return

    head_lock = head_path.with_name(head_path.name + ".lock")
    index_lock = index_path.with_name(index_path.name + ".lock")
    published: list[tuple[Path, Path, dict[str, int | str]]] = []
    try:
        _publish_journal_lock(rollback_head_source, rollback_head_record, head_lock)
        published.append((head_lock, rollback_head_source, rollback_head_record))
        _publish_journal_lock(rollback_index_source, rollback_index_record, index_lock)
        published.append((index_lock, rollback_index_source, rollback_index_record))
        # The hard-linked locks make this second comparison a true CAS boundary.
        current_head = _read_regular_file(head_path, label="Git HEAD")
        current_index = _read_optional_regular_file(index_path, label="Git index")
        if current_head not in {original_head, checkpoint_head}:
            raise GitStateError("Git HEAD changed outside the archived checkpoint")
        if current_index not in {original_index, checkpoint_data}:
            raise GitStateError("Git index changed outside the archived checkpoint")
        if current_index == checkpoint_data:
            if original_index is None:
                index_path.unlink(missing_ok=True)
                _fsync_directory(index_path.parent)
                _reclaim_journal_lock(
                    index_lock, rollback_index_source, rollback_index_record
                )
            else:
                os.replace(index_lock, index_path)
                _fsync_directory(index_path.parent)
        else:
            _reclaim_journal_lock(index_lock, rollback_index_source, rollback_index_record)
        if current_head == checkpoint_head:
            os.replace(head_lock, head_path)
            _fsync_directory(head_path.parent)
        else:
            _reclaim_journal_lock(head_lock, rollback_head_source, rollback_head_record)
    except BaseException:
        for lock, source, record in reversed(published):
            _reclaim_journal_lock(lock, source, record)
        raise


def _journal_path(root: Path, worktree: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(str(worktree))).hexdigest()
    return root / ".awf-archive-removal-checkpoints" / digest


def _journal_lock_sources(journal: Path) -> dict[str, Path]:
    return {
        "checkpoint_head": journal / "checkpoint-head",
        "checkpoint_index": journal / "checkpoint-index",
        "rollback_head": journal / "original-head",
        "rollback_index": (
            journal / "original-index"
            if (journal / "original-index").exists()
            else journal / "rollback-index"
        ),
    }


def _read_journal(journal: Path) -> dict[str, Any]:
    _require_private_directory(journal, label="checkpoint journal")
    raw = _read_regular_file(journal / "journal.json", label="checkpoint journal")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitStateError("checkpoint journal is malformed") from error
    required = {
        "version",
        "worktree",
        "git_dir",
        "index_path",
        "git_state",
        "original_head",
        "original_index",
        "checkpoint_head",
        "checkpoint_index",
        "locks",
    }
    if not isinstance(value, dict) or set(value) != required or value["version"] != 2:
        raise GitStateError("checkpoint journal is invalid")
    if not all(isinstance(value[key], str) for key in ("worktree", "git_dir", "index_path", "checkpoint_head")):
        raise GitStateError("checkpoint journal is invalid")
    _normalise_artifacts(value["git_state"])
    _normalise_file_record(value["original_head"])
    if value["original_index"] is not None:
        _normalise_file_record(value["original_index"])
    _normalise_file_record(value["checkpoint_index"])
    _normalise_lock_records(value["locks"])
    return value


def _normalise_lock_records(value: Any) -> None:
    names = {"checkpoint_head", "checkpoint_index", "rollback_head", "rollback_index"}
    if not isinstance(value, dict) or set(value) != names:
        raise GitStateError("checkpoint lock records are invalid")
    for record in value.values():
        if (
            not isinstance(record, dict)
            or set(record) != _LOCK_RECORD_FIELDS
            or not isinstance(record["device"], int)
            or isinstance(record["device"], bool)
            or record["device"] < 0
            or not isinstance(record["inode"], int)
            or isinstance(record["inode"], bool)
            or record["inode"] < 0
        ):
            raise GitStateError("checkpoint lock record is invalid")
        _normalise_file_record(record, expected_fields=_LOCK_RECORD_FIELDS)


def _lock_record(path: Path, *, label: str) -> dict[str, int | str]:
    details = _regular_lstat(path, label)
    if (
        details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink not in {1, 2}
    ):
        raise GitStateError(f"{label} is not a private checkpoint lock")
    return {
        **_file_record(path, required=True, label=label),
        "device": details.st_dev,
        "inode": details.st_ino,
    }


def _validate_journal_locks(
    sources: dict[str, Path], locks: dict[str, dict[str, int | str]]
) -> None:
    _normalise_lock_records(locks)
    for name, source in sources.items():
        if _lock_record(source, label=f"checkpoint {name} lock") != locks[name]:
            raise GitStateError("checkpoint lock backing changed unexpectedly")


def _publish_journal_lock(source: Path, record: dict[str, int | str], target: Path) -> None:
    if _lock_record(source, label="checkpoint lock backing") != record:
        raise GitStateError("checkpoint lock backing changed unexpectedly")
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as error:
        raise GitStateError("Git HEAD or index is locked") from error
    try:
        if _lock_record(target, label="checkpoint lock") != record:
            raise GitStateError("checkpoint lock identity changed unexpectedly")
        if source.lstat().st_nlink != 2 or target.lstat().st_nlink != 2:
            raise GitStateError("checkpoint lock has an unexpected link count")
        _fsync_directory(target.parent)
    except BaseException:
        _reclaim_journal_lock(target, source, record)
        raise


def _reclaim_journal_lock(target: Path, source: Path, record: dict[str, int | str]) -> None:
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise GitStateError("unable to inspect Git state lock") from error
    if _lock_record(source, label="checkpoint lock backing") != record:
        raise GitStateError("checkpoint lock backing changed unexpectedly")
    if _lock_record(target, label="checkpoint lock") != record:
        raise GitStateError("Git HEAD or index has a foreign lock")
    if source.lstat().st_nlink != 2 or target.lstat().st_nlink != 2:
        raise GitStateError("checkpoint lock has an unexpected link count")
    try:
        target.unlink()
        _fsync_directory(target.parent)
    except OSError as error:
        raise GitStateError("unable to reclaim checkpoint lock") from error


# -- snapshot collection ----------------------------------------------------


def _git_paths(
    git: GitClient, worktree: Path, environment: dict[str, str]
) -> tuple[Path, Path, Path]:
    git_dir = Path(
        _git_text(
            git,
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            cwd=worktree,
            env=environment,
        )
    ).resolve()
    index_path = Path(
        _git_text(
            git,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "index",
            cwd=worktree,
            env=environment,
        )
    ).resolve()
    objects = Path(
        _git_text(
            git,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
            cwd=worktree,
            env=environment,
        )
    ).resolve()
    _require_git_directory(git_dir, label="Git directory")
    _require_git_directory(objects, label="Git object directory")
    return git_dir, index_path, objects


def _parse_head(
    git: GitClient,
    worktree: Path,
    raw: bytes,
    oid_length: int,
    environment: dict[str, str],
) -> dict[str, str]:
    if raw.startswith(b"ref: ") and raw.endswith(b"\n"):
        try:
            ref = raw[5:-1].decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise GitStateError("Git HEAD reference is invalid") from error
        if not _safe_branch_ref(ref):
            raise GitStateError("Git HEAD reference is unsafe")
        oid = _git_text(git, "rev-parse", "--verify", "HEAD", cwd=worktree, env=environment)
        if not _is_object_id(oid, oid_length):
            raise GitStateError("Git HEAD points to an invalid object")
        return {"kind": "symbolic", "ref": ref, "oid": oid}
    if raw.endswith(b"\n"):
        try:
            oid = raw[:-1].decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise GitStateError("Git detached HEAD is invalid") from error
        if _is_object_id(oid, oid_length):
            return {"kind": "detached", "oid": oid}
    raise GitStateError("Git HEAD has an unsupported representation")


def _index_object_ids(
    git: GitClient, worktree: Path, oid_length: int, environment: dict[str, str]
) -> list[str]:
    output = _git(git, "ls-files", "--stage", "-z", cwd=worktree, env=environment)
    return _parse_index_ids(output, oid_length)


def _parse_index_ids(output: bytes, oid_length: int) -> list[str]:
    result: set[str] = set()
    for item in output.split(b"\0"):
        if not item:
            continue
        metadata, separator, _path = item.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise GitStateError("Git index returned an invalid entry")
        if fields[0] == b"160000":
            # A gitlink's commit belongs to its submodule object database, not this
            # repository. The raw index retains it; it cannot be packed here.
            continue
        try:
            object_id = fields[1].decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise GitStateError("Git index returned an invalid object id") from error
        if not _is_object_id(object_id, oid_length):
            raise GitStateError("Git index returned an invalid object id")
        result.add(object_id)
    return sorted(result)


def _operation_metadata(git_dir: Path) -> list[tuple[dict[str, Any], Path]]:
    records: list[tuple[dict[str, Any], Path]] = []
    total = 0
    for name in _DIRECT_OPERATION_FILES:
        path = git_dir / name
        if path.exists() or path.is_symlink():
            total += _append_metadata_file(records, name, path)
    for directory in _OPERATION_DIRECTORIES:
        root = git_dir / directory
        if not root.exists() and not root.is_symlink():
            continue
        if root.is_symlink() or not root.is_dir():
            raise GitStateError("Git operation directory is unsafe")
        for name, path in _walk_metadata_directory(root, directory):
            total += _append_metadata_file(records, name, path)
    if total > _MAX_METADATA_BYTES:
        raise GitStateError("Git operation metadata exceeds the safe size limit")
    records.sort(key=lambda item: item[0]["name"])
    if len({record["name"] for record, _path in records}) != len(records):
        raise GitStateError("Git operation metadata contains duplicate names")
    return records


def _append_metadata_file(records: list[tuple[dict[str, Any], Path]], name: str, path: Path) -> int:
    data = _read_regular_file(path, label="Git operation metadata")
    record = {"name": name, **_bytes_record(data)}
    records.append((record, path))
    return len(data)


def _walk_metadata_directory(root: Path, prefix: str) -> Iterator[tuple[str, Path]]:
    try:
        with os.scandir(root) as scanner:
            children = sorted(scanner, key=lambda entry: entry.name)
    except OSError as error:
        raise GitStateError("unable to inspect Git operation metadata") from error
    for entry in children:
        name = f"{prefix}/{entry.name}"
        path = Path(entry.path)
        try:
            details = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise GitStateError("unable to inspect Git operation metadata") from error
        if stat.S_ISDIR(details.st_mode):
            yield from _walk_metadata_directory(path, name)
        elif stat.S_ISREG(details.st_mode):
            yield name, path
        else:
            raise GitStateError("Git operation metadata contains an unsafe entry")


def _metadata_object_ids(
    git: GitClient,
    worktree: Path,
    metadata: list[tuple[dict[str, Any], Path]],
    oid_length: int,
    environment: dict[str, str],
) -> list[str]:
    expression = re.compile(rb"(?<![0-9a-f])([0-9a-f]{" + str(oid_length).encode("ascii") + rb"})(?![0-9a-f])")
    candidates: set[str] = set()
    for _record, path in metadata:
        data = _read_regular_file(path, label="Git operation metadata")
        for match in expression.finditer(data):
            candidates.add(match.group(1).decode("ascii"))
    present: list[str] = []
    for candidate in sorted(candidates):
        try:
            _git(
                git,
                "cat-file",
                "-e",
                f"{candidate}^{{object}}",
                cwd=worktree,
                env=environment,
            )
        except GitStateError:
            continue
        present.append(candidate)
    return present


# -- safe filesystem and Git process helpers --------------------------------


def _neutral_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _seal_private_file(path: Path, *, label: str) -> None:
    before = _regular_lstat(path, label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GitStateError(f"{label} cannot be secured") from error
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise GitStateError(f"{label} changed while it was secured")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError as error:
        raise GitStateError(f"{label} cannot be secured") from error
    finally:
        os.close(descriptor)
    _validate_private_file(path, label=label)


def _checkpoint_environment(
    object_directory: Path, common_objects: Path, *, index_path: Path | None = None
) -> dict[str, str]:
    environment = _neutral_environment()
    environment["GIT_OBJECT_DIRECTORY"] = str(object_directory)
    environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(common_objects)
    if index_path is not None:
        environment["GIT_INDEX_FILE"] = str(index_path)
    return environment


def _git(
    git: GitClient,
    *arguments: str,
    cwd: Path,
    env: dict[str, str],
    input_bytes: bytes | None = None,
) -> bytes:
    try:
        return git._run(  # pyright: ignore[reportPrivateUsage]
            *_GIT_PREFIX, *arguments, cwd=cwd, env=env, input_bytes=input_bytes
        ).stdout
    except GitError as error:
        raise GitStateError("Git state command failed") from error


def _git_text(
    git: GitClient,
    *arguments: str,
    cwd: Path,
    env: dict[str, str],
    input_bytes: bytes | None = None,
) -> str:
    try:
        return _git(git, *arguments, cwd=cwd, env=env, input_bytes=input_bytes).decode(
            "utf-8", errors="strict"
        ).strip()
    except UnicodeDecodeError as error:
        raise GitStateError("Git state command returned invalid text") from error


def _private_git(
    *arguments: str, cwd: Path, env: dict[str, str], input_bytes: bytes | None = None
) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *_GIT_PREFIX, *arguments],
            cwd=str(cwd),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitStateError("private Git verification command failed") from error
    if completed.returncode != 0:
        raise GitStateError("private Git verification command was rejected")
    return completed.stdout


def _require_worktree(path: Path) -> Path:
    supplied = Path(path)
    try:
        details = supplied.lstat()
        resolved = supplied.resolve(strict=True)
        resolved_details = resolved.lstat()
    except OSError as error:
        raise GitStateError("worktree is unavailable") from error
    if (
        supplied.is_symlink()
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(resolved_details.st_mode)
    ):
        raise GitStateError("worktree must be a non-symlink directory")
    return resolved


def _require_private_directory(path: Path, *, label: str) -> Path:
    supplied = Path(path)
    try:
        details = supplied.lstat()
        resolved = supplied.resolve(strict=True)
        resolved_details = resolved.lstat()
    except OSError as error:
        raise GitStateError(f"{label} is unavailable") from error
    if (
        supplied.is_symlink()
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(resolved_details.st_mode)
        or resolved_details.st_uid != os.getuid()
        or stat.S_IMODE(resolved_details.st_mode) != 0o700
    ):
        raise GitStateError(f"{label} is not private")
    return resolved

def _require_git_directory(path: Path, *, label: str) -> Path:
    supplied = Path(path)
    try:
        details = supplied.lstat()
        resolved = supplied.resolve(strict=True)
        resolved_details = resolved.lstat()
    except OSError as error:
        raise GitStateError(f"{label} is unavailable") from error
    if (
        supplied.is_symlink()
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(resolved_details.st_mode)
        or resolved_details.st_uid != os.getuid()
        or resolved_details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise GitStateError(f"{label} is unsafe")
    return resolved


def _validate_private_file(path: Path, *, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise GitStateError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
    ):
        raise GitStateError(f"{label} is not a private regular file")


def _read_regular_file(path: Path, *, label: str) -> bytes:
    before = _regular_lstat(path, label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GitStateError(f"{label} cannot be opened") from error
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise GitStateError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    except OSError as error:
        raise GitStateError(f"{label} cannot be read") from error
    finally:
        os.close(descriptor)
    after = _regular_lstat(path, label)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise GitStateError(f"{label} changed while it was read")
    return data


def _read_optional_regular_file(path: Path, *, label: str) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise GitStateError(f"{label} is unavailable") from error
    return _read_regular_file(path, label=label)


def _regular_lstat(path: Path, label: str) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as error:
        raise GitStateError(f"{label} is unavailable") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise GitStateError(f"{label} is not a regular file")
    return details


def _file_record(path: Path, *, required: bool, label: str) -> dict[str, Any]:
    try:
        details = _regular_lstat(path, label)
    except GitStateError:
        if required:
            raise
        try:
            path.lstat()
        except FileNotFoundError:
            return {"present": False}
        raise
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GitStateError(f"{label} cannot be opened") from error
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != details.st_dev or opened.st_ino != details.st_ino:
            raise GitStateError(f"{label} changed while it was opened")
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
    except OSError as error:
        raise GitStateError(f"{label} cannot be read") from error
    finally:
        os.close(descriptor)
    final = _regular_lstat(path, label)
    if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    ):
        raise GitStateError(f"{label} changed while it was read")
    record = {"bytes": count, "sha256": digest.hexdigest()}
    return record if required else {"present": True, **record}


def _bytes_record(value: bytes) -> dict[str, int | str]:
    return {"bytes": len(value), "sha256": _sha256(value)}


def _without_present(value: dict[str, Any]) -> dict[str, Any]:
    return {"bytes": value["bytes"], "sha256": value["sha256"]}


def _open_private_output(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor
    except OSError as error:
        raise GitStateError("unable to create private Git state artifact") from error


def _write_new_file(path: Path, data: bytes) -> None:
    descriptor = _open_private_output(path)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _validate_private_file(path, label="private Git state file")


def _write_replace_file(path: Path, data: bytes) -> None:
    lock = path.with_name(f".{path.name}.awf-restore-{os.urandom(12).hex()}")
    _write_new_file(lock, data)
    try:
        os.replace(lock, path)
        _fsync_directory(path.parent)
    except OSError as error:
        raise GitStateError("unable to restore Git operation metadata") from error


def _add_tar_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.mtime = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def _private_artifact_record(path: Path) -> dict[str, int | str]:
    _validate_private_file(path, label="Git state artifact")
    return _file_record(path, required=True, label="Git state artifact")


def _copy_private_file(source: Path, target: Path) -> None:
    _validate_private_file(source, label="Git state artifact")
    descriptor = _open_private_output(target)
    try:
        input_descriptor = os.open(
            source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            while True:
                block = os.read(input_descriptor, _COPY_CHUNK)
                if not block:
                    break
                _write_all(descriptor, block)
            os.fsync(descriptor)
        finally:
            os.close(input_descriptor)
    except OSError as error:
        raise GitStateError("unable to copy private Git object pack") from error
    finally:
        os.close(descriptor)
    _validate_private_file(target, label="copied Git object pack")


def _install_pack(
    source: Path,
    object_directory: Path,
    destination: Path,
    environment: dict[str, str],
) -> None:
    digest = _private_artifact_record(source)["sha256"]
    if not isinstance(digest, str):
        raise GitStateError("Git object pack checksum is invalid")
    pack_dir = object_directory / "pack"
    _require_git_directory(pack_dir, label="destination pack directory")
    target = pack_dir / f"pack-awf-git-state-{digest}.pack"
    _copy_private_file(source, target)
    _private_git("index-pack", "--strict", str(target), cwd=destination, env=environment)
    index = target.with_suffix(".idx")
    _seal_private_file(index, label="restored Git object index")
    _fsync_directory(pack_dir)


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("unable to write Git state data")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GitStateError("unable to sync private Git state directory") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise GitStateError("unable to sync private Git state directory") from error
    finally:
        os.close(descriptor)

def _fsync_private_tree(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise GitStateError("checkpoint object store is unavailable") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise GitStateError("checkpoint object store is unsafe")
    try:
        with os.scandir(path) as scanner:
            children = list(scanner)
    except OSError as error:
        raise GitStateError("checkpoint object store cannot be inspected") from error
    for child in children:
        child_path = Path(child.path)
        try:
            child_details = child.stat(follow_symlinks=False)
        except OSError as error:
            raise GitStateError("checkpoint object store changed unexpectedly") from error
        if stat.S_ISDIR(child_details.st_mode):
            _fsync_private_tree(child_path)
        elif stat.S_ISREG(child_details.st_mode) and not stat.S_ISLNK(child_details.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(child_path, flags)
            except OSError as error:
                raise GitStateError("checkpoint object cannot be opened") from error
            try:
                opened = os.fstat(descriptor)
                if opened.st_dev != child_details.st_dev or opened.st_ino != child_details.st_ino:
                    raise GitStateError("checkpoint object changed unexpectedly")
                os.fsync(descriptor)
            except OSError as error:
                raise GitStateError("checkpoint object cannot be synced") from error
            finally:
                os.close(descriptor)
        else:
            raise GitStateError("checkpoint object store contains an unsafe entry")
    _fsync_directory(path)


def _assert_safe_directory_chain(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise GitStateError("Git operation metadata escapes the destination") from error
    current = root
    for component in path.relative_to(root).parts:
        current /= component
        try:
            details = current.lstat()
        except OSError as error:
            raise GitStateError("Git operation metadata directory is unavailable") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise GitStateError("Git operation metadata directory is unsafe")


def _metadata_destination(git_dir: Path, name: str) -> Path:
    if not _safe_metadata_name(name):
        raise GitStateError("Git operation metadata destination is invalid")
    destination = git_dir.joinpath(*name.split("/"))
    try:
        destination.relative_to(git_dir)
    except ValueError as error:
        raise GitStateError("Git operation metadata escapes the destination") from error
    return destination


def _clear_operation_metadata(git_dir: Path) -> None:
    for name in _DIRECT_OPERATION_FILES:
        path = git_dir / name
        try:
            details = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise GitStateError("unable to inspect destination Git operation metadata") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise GitStateError("destination Git operation metadata is unsafe")
        path.unlink()
    for name in _OPERATION_DIRECTORIES:
        path = git_dir / name
        try:
            details = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise GitStateError("unable to inspect destination Git operation metadata") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise GitStateError("destination Git operation metadata is unsafe")
        shutil.rmtree(path)


def _replace_index(path: Path, value: bytes | None) -> None:
    lock = path.with_name(path.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        raise GitStateError("unable to lock destination Git index") from error
    try:
        if value is None:
            os.close(descriptor)
            descriptor = -1
            path.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
        else:
            _write_all(descriptor, value)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(lock, path)
            _fsync_directory(path.parent)
    except OSError as error:
        raise GitStateError("unable to restore destination Git index") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _walk_checkpoint_files(
    root: Path, excluded_paths: tuple[str, ...]
) -> Iterator[tuple[str, Path, os.stat_result]]:
    excluded = set(excluded_paths)

    def walk(directory: Path, relative: str) -> Iterator[tuple[str, Path, os.stat_result]]:
        try:
            with os.scandir(directory) as scanner:
                children = sorted(scanner, key=lambda item: item.name)
        except OSError as error:
            raise GitStateError("unable to traverse checkpoint worktree") from error
        for entry in children:
            if entry.name == ".git":
                if not relative:
                    continue
                raise GitStateError("checkpoint worktree contains a nested Git repository")
            name = entry.name if not relative else f"{relative}/{entry.name}"
            if name in excluded:
                continue
            try:
                name.encode("utf-8", errors="strict")
                details = entry.stat(follow_symlinks=False)
            except (OSError, UnicodeError) as error:
                raise GitStateError("checkpoint worktree contains an unsafe path") from error
            path = Path(entry.path)
            if stat.S_ISDIR(details.st_mode):
                yield from walk(path, name)
            elif stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
                yield name, path, details
            else:
                raise GitStateError("checkpoint worktree has an unsupported entry")
    yield from walk(root, "")


def _hash_regular_file(
    git: GitClient,
    path: Path,
    expected: os.stat_result,
    cwd: Path,
    environment: dict[str, str],
    oid_length: int,
) -> str:
    process: subprocess.Popen[bytes] | None = None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GitStateError("unable to open checkpoint worktree file") from error
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
            raise GitStateError("checkpoint worktree file changed while opened")
        try:
            process = subprocess.Popen(
                ["git", *_GIT_PREFIX, "hash-object", "-w", "--no-filters", "--stdin"],
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=environment,
            )
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise GitStateError("unable to hash checkpoint worktree file")
            while True:
                chunk = os.read(descriptor, _COPY_CHUNK)
                if not chunk:
                    break
                process.stdin.write(chunk)
            process.stdin.close()
            output = process.stdout.read()
            _stderr = process.stderr.read()
            process.wait(timeout=git.timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            if process is not None:
                _stop_process(process)
            raise GitStateError("unable to hash checkpoint worktree file") from error
        except BaseException:
            if process is not None:
                _stop_process(process)
            raise
        finally:
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
        if process is None or process.returncode != 0:
            raise GitStateError("unable to hash checkpoint worktree file")
    finally:
        os.close(descriptor)
    final = _regular_lstat(path, "checkpoint worktree file")
    if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
    ):
        raise GitStateError("checkpoint worktree file changed while hashed")
    try:
        object_id = output.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise GitStateError("Git returned an invalid checkpoint object") from error
    if not _is_object_id(object_id, oid_length):
        raise GitStateError("Git returned an invalid checkpoint object")
    return object_id


def _hash_bytes(
    git: GitClient,
    value: bytes,
    cwd: Path,
    environment: dict[str, str],
    oid_length: int,
) -> str:
    output = _git(
        git,
        "hash-object",
        "-w",
        "--no-filters",
        "--stdin",
        cwd=cwd,
        env=environment,
        input_bytes=value,
    )
    try:
        object_id = output.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise GitStateError("Git returned an invalid checkpoint object") from error
    if not _is_object_id(object_id, oid_length):
        raise GitStateError("Git returned an invalid checkpoint object")
    return object_id


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _remove_private_tree(path: Path) -> None:
    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise GitStateError("checkpoint journal is unsafe")
        shutil.rmtree(path)
        _fsync_directory(path.parent)
    except OSError as error:
        raise GitStateError("unable to remove completed checkpoint journal") from error


def _safe_metadata_name(value: str) -> bool:
    return (
        bool(value)
        and "\0" not in value
        and not value.startswith("/")
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and (value in _DIRECT_OPERATION_FILES or value.split("/", 1)[0] in _OPERATION_DIRECTORIES)
    )


def _safe_branch_ref(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("refs/heads/")
        and len(value) > len("refs/heads/")
        and "\0" not in value
        and not value.endswith("/")
        and not value.endswith(".")
        and ".." not in value
        and "//" not in value
        and "@{" not in value
        and not any(ord(character) < 32 or character in " ~^:?*[\\" for character in value)
    )


def _is_object_id(value: Any, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _oid_length(snapshot: dict[str, Any]) -> int:
    return _OBJECT_FORMATS[snapshot["object_format"]]


def _oid_length_from_head(raw: bytes, git: GitClient, worktree: Path, environment: dict[str, str]) -> int:
    # Recovery never relies on the source object's contents; object format is the
    # only live repository fact required to validate a recorded checkpoint id.
    value = _git_text(git, "rev-parse", "--show-object-format", cwd=worktree, env=environment)
    length = _OBJECT_FORMATS.get(value)
    if length is None:
        raise GitStateError("Git uses an unsupported object format")
    return length


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise GitStateError("Git state cannot be represented safely") from error
