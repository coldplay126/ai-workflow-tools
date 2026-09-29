from __future__ import annotations

from collections.abc import Mapping

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from awf.cli import main

import awf.worktrees.archive as archive_module
import awf.worktrees.archive_discard as archive_discard_module
from awf.worktrees.archive import (
    ArchiveError,
    create_archive,
    read_verified_archive,
    restore_archive,
    snapshot_worktree,
    validate_backup_root,
)
from awf.worktrees.archive_repack import ArchiveRepacker
from awf.worktrees.git import GitClient, GitError
from awf.worktrees.git_state import snapshot_git_state
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from test_worktree_archive import ArchiveHarness, _backup_root
from worktree_fixtures import git, make_repository


_REASON = "archive state regression fixture"


def _metadata(git_client: GitClient, worktree: Path) -> dict[str, object]:
    return {
        "head_sha": git_client.head_sha(worktree),
        "lease": {"id": "archive-state-fixture"},
        "repository_id": "archive-state-fixture",
        "reason": _REASON,
        "preview_token": "0" * 64,
    }


def _git_output(
    path: Path, *arguments: str, environment: Mapping[str, str] | None = None
) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout


def _git_result(
    path: Path, *arguments: str, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        input=input_bytes,
        check=False,
        capture_output=True,
    )


def _preview_token(result: object) -> str:
    actions = getattr(result, "actions")
    for action in actions:
        token = action.get("preview_token")
        if isinstance(token, str):
            return token
    raise AssertionError("preview did not provide a token")


def _archive_destination(result: object) -> Path:
    actions = getattr(result, "actions")
    for action in actions:
        destination = action.get("backup_directory")
        if isinstance(destination, str):
            return Path(destination)
    raise AssertionError("preview did not provide an archive destination")


def _archive_discard_preview(
    harness: ArchiveHarness,
    *,
    lease_id: str,
    backup_root: Path,
    include_uncommitted: bool = False,
    exclude_ignored_paths: tuple[str, ...] = (),
) -> tuple[str, Path]:
    preview = harness.service.archive_discard(
        lease_id,
        backup_root=backup_root,
        reason=_REASON,
        include_uncommitted=include_uncommitted,
        exclude_ignored_paths=exclude_ignored_paths,
    )
    assert preview.status == "ok"
    assert preview.decision == "preview"
    return _preview_token(preview), _archive_destination(preview)


def _archive_discard_apply(
    harness: ArchiveHarness,
    *,
    lease_id: str,
    backup_root: Path,
    preview_token: str,
    include_uncommitted: bool = False,
    exclude_ignored_paths: tuple[str, ...] = (),
    service: WorktreeService | None = None,
):
    return (service or harness.service).archive_discard(
        lease_id,
        backup_root=backup_root,
        reason=_REASON,
        preview_token=preview_token,
        include_uncommitted=include_uncommitted,
        exclude_ignored_paths=exclude_ignored_paths,
        apply=True,
    )


def _fresh_archive_service(harness: ArchiveHarness) -> WorktreeService:
    return WorktreeService(
        WorktreeRegistry(harness.registry.db_path),
        GitClient(harness.repo),
        config=harness.service.config,
        github=harness.github,
        cache_dir=harness.cache_dir,
        state_dir=harness.service.state_dir,
        lock_dir=harness.service.lock_dir,
        home_dir=harness.service.home_dir,
    )

def _archive_repacker(harness: ArchiveHarness) -> ArchiveRepacker:
    return ArchiveRepacker(
        registry=harness.registry,
        git=harness.git,
        cache_dir=harness.cache_dir,
        lock_dir=harness.service.lock_dir,
    )


def _archive_file_bytes(root: Path) -> dict[Path, bytes]:
    return {
        candidate.relative_to(root): candidate.read_bytes()
        for candidate in sorted(root.rglob("*"))
        if candidate.is_file()
    }

def _snapshot_with_entries(
    snapshot: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    result = {
        **snapshot,
        "entries": sorted(entries, key=lambda entry: entry["name"]),
    }
    result["total_bytes"] = sum(
        entry["size"] for entry in result["entries"] if entry["type"] == "regular"
    )
    details: dict[str, Any] = {
        "entries": result["entries"],
        "total_bytes": result["total_bytes"],
        "root": result["root"],
    }
    if "excluded_paths" in result:
        details["excluded_paths"] = result["excluded_paths"]
    result["fingerprint"] = hashlib.sha256(
        json.dumps(
            details,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", "strict")
    ).hexdigest()
    return result


def _rewrite_as_legacy_archive(archive_path: Path) -> None:
    manifest_path = archive_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest_path.write_text(
        json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)


def _conflicted_dirty_repository(tmp_path: Path) -> tuple[Path, GitClient]:
    repository = make_repository(tmp_path)
    git_client = GitClient(repository)
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repository / "skip.txt").write_text("skip\n", encoding="utf-8")
    (repository / "assume.txt").write_text("assume\n", encoding="utf-8")
    (repository / "conflict.txt").write_text("base\n", encoding="utf-8")
    git(repository, "add", "tracked.txt", "skip.txt", "assume.txt", "conflict.txt")
    git(repository, "commit", "-q", "-m", "add state fixtures")
    git(repository, "branch", "archive-state-side")

    (repository / "conflict.txt").write_text("current\n", encoding="utf-8")
    git(repository, "commit", "-am", "current conflict side", "-q")
    git(repository, "checkout", "-q", "archive-state-side")
    (repository / "conflict.txt").write_text("other\n", encoding="utf-8")
    git(repository, "commit", "-am", "other conflict side", "-q")
    git(repository, "checkout", "-q", "staging")
    merge = _git_result(repository, "merge", "archive-state-side")
    assert merge.returncode != 0

    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    (repository / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (repository / "operator-note.txt").write_text("untracked\n", encoding="utf-8")
    git(repository, "update-index", "--skip-worktree", "skip.txt")
    git(repository, "update-index", "--assume-unchanged", "assume.txt")
    return repository, git_client


def test_safe_node_modules_exclusion_restores_without_the_source_repository(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    git_client = GitClient(repository)
    (repository / ".gitignore").write_text("node_modules/\n*.env\n", encoding="utf-8")
    (repository / "retained.txt").write_text("must survive\n", encoding="utf-8")
    git(repository, "add", ".gitignore", "retained.txt")
    git(repository, "commit", "-q", "-m", "add archive filter fixture")
    (repository / "node_modules" / "package").mkdir(parents=True)
    (repository / "node_modules" / "package" / "index.js").write_text(
        "discarded dependency\n", encoding="utf-8"
    )
    (repository / "operator.env").write_text("preserve ignored file\n", encoding="utf-8")

    backup_root = _backup_root(tmp_path)
    archive_path = backup_root / "legacy-archive"
    snapshot = snapshot_worktree(
        repository, exclude_ignored_paths=("node_modules",)
    )
    create_archive(
        destination=archive_path,
        worktree_path=repository,
        snapshot=snapshot,
        metadata=_metadata(git_client, repository),
        git=git_client,
    )

    expected_head = git_client.head_sha(repository)
    shutil.rmtree(repository)
    shutil.rmtree(tmp_path / "origin.git")
    restore_root = _backup_root(tmp_path, "restored")
    destination = restore_root / "worktree"
    restore_archive(archive_path=archive_path, destination=destination)

    assert git(destination, "rev-parse", "HEAD") == expected_head
    assert (destination / "retained.txt").read_text(encoding="utf-8") == "must survive\n"
    assert (destination / "operator.env").read_text(encoding="utf-8") == (
        "preserve ignored file\n"
    )
    assert not (destination / "node_modules").exists()


@pytest.mark.parametrize("invalid", ("exists", "missing_parent", "public_parent"))
def test_archive_restore_preview_rejects_unsafe_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], invalid: str
) -> None:
    repository = make_repository(tmp_path)
    git_client = GitClient(repository)
    archive_path = _backup_root(tmp_path) / "archive"
    create_archive(
        destination=archive_path,
        worktree_path=repository,
        snapshot=snapshot_worktree(repository),
        metadata=_metadata(git_client, repository),
        git=git_client,
    )
    parent = tmp_path / "missing" if invalid == "missing_parent" else tmp_path / "restore"
    if invalid != "missing_parent":
        parent.mkdir(mode=0o700)
    if invalid == "public_parent":
        parent.chmod(0o755)
    destination = parent / "worktree"
    if invalid == "exists":
        destination.mkdir()

    exit_code = main([
        "wt", "archive-restore", "--archive", str(archive_path),
        "--destination", str(destination), "--json",
    ])

    result = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert result["decision"] == "blocked"
    assert result["blockers"][0]["code"] == {
        "exists": "restore_destination_exists",
        "missing_parent": "backup_root_invalid",
        "public_parent": "backup_root_unsafe",
    }[invalid]
    assert destination.exists() is (invalid == "exists")


@pytest.mark.parametrize("unsafe_case", ("tracked", "symlink"))
def test_node_modules_exclusion_rejects_tracked_or_symlinked_paths(
    tmp_path: Path, unsafe_case: str
) -> None:
    repository = make_repository(tmp_path)
    (repository / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-q", "-m", "ignore dependencies")

    node_modules = repository / "node_modules"
    if unsafe_case == "tracked":
        node_modules.mkdir()
        (node_modules / "tracked.js").write_text("tracked\n", encoding="utf-8")
        git(repository, "add", "-f", "node_modules/tracked.js")
        git(repository, "commit", "-q", "-m", "track dependency fixture")
    else:
        target = repository / "real-node-modules"
        target.mkdir()
        (target / "index.js").write_text("linked\n", encoding="utf-8")
        node_modules.symlink_to(target.name, target_is_directory=True)

    with pytest.raises(ArchiveError):
        snapshot_worktree(repository, exclude_ignored_paths=("node_modules",))


@pytest.mark.parametrize("mutation", ("symlink_parent", "git_injection"))
def test_create_archive_rejects_tampered_snapshot_topology(
    tmp_path: Path, mutation: str
) -> None:
    repository = make_repository(tmp_path)
    git_client = GitClient(repository)
    snapshot = snapshot_worktree(repository)
    entries = [dict(entry) for entry in snapshot["entries"]]

    if mutation == "symlink_parent":
        regular_index = next(
            index
            for index, entry in enumerate(entries)
            if entry["type"] == "regular"
        )
        child = entries.pop(regular_index)
        child["name"] = "linked/child.txt"
        entries.extend(
            (
                {
                    "name": "linked",
                    "type": "symlink",
                    "mode": 0o777,
                    "link_target": ".",
                },
                child,
            )
        )
    else:
        git_dir = repository / ".git"
        git_head = git_dir / "HEAD"
        entries.extend(
            (
                {
                    "name": ".GiT",
                    "type": "directory",
                    "mode": stat.S_IMODE(git_dir.lstat().st_mode),
                },
                {
                    "name": ".GiT/HEAD",
                    "type": "regular",
                    "mode": stat.S_IMODE(git_head.lstat().st_mode),
                    "size": git_head.stat().st_size,
                    "hash": hashlib.sha256(git_head.read_bytes()).hexdigest(),
                },
            )
        )

    archive_path = _backup_root(tmp_path) / f"tampered-{mutation}"
    with pytest.raises(ArchiveError) as error:
        create_archive(
            destination=archive_path,
            worktree_path=repository,
            snapshot=_snapshot_with_entries(snapshot, entries),
            metadata=_metadata(git_client, repository),
            git=git_client,
        )

    assert error.value.code == "snapshot_invalid"
    assert not archive_path.exists()


def test_repack_rejects_ambient_git_ignore_policy_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("ambient-git-policy")
    worktree = lease.worktree_path
    common_git_dir = Path(
        git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    (common_git_dir / "info" / "exclude").write_text(
        "node_modules/\n", encoding="utf-8"
    )
    node_modules = worktree / "node_modules" / "package"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("must remain\n", encoding="utf-8")

    backup_root = _backup_root(tmp_path)
    archive_token, archive_path = _archive_discard_preview(
        harness, lease_id=lease.id, backup_root=backup_root
    )
    archived = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=archive_token,
    )
    assert archived.status == "ok"
    global_ignore = tmp_path / "ambient-ignore"
    global_ignore.write_text("node_modules/\n", encoding="utf-8")
    global_config = tmp_path / "ambient-gitconfig"
    global_config.write_text(
        f"[core]\n\texcludesFile = {global_ignore}\n", encoding="utf-8"
    )
    repacked_path = backup_root / "repacked"
    repacker = _archive_repacker(harness)

    with monkeypatch.context() as ambient:
        ambient.setenv("GIT_DIR", str(harness.repo / ".git"))
        ambient.setenv("GIT_INDEX_FILE", str(tmp_path / "ambient-index"))
        ambient.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        preview = repacker.run(
            archive_path, exclude_ignored_paths=("node_modules",), apply=False
        )
        assert preview.status == "blocked"
        assert preview.blockers[0]["code"] == "excluded_path_unverified"
        with pytest.raises(ArchiveError) as error:
            archive_module.repack_archive_contents(
                source=archive_path,
                destination=repacked_path,
                exclude_ignored_paths=("node_modules",),
            )

    assert error.value.code == "excluded_path_unverified"
    assert not repacked_path.exists()


def test_archive_restores_conflict_and_index_state_without_its_source_repository(
    tmp_path: Path,
) -> None:
    repository, git_client = _conflicted_dirty_repository(tmp_path)
    expected_head = _git_output(repository, "rev-parse", "HEAD")
    expected_status = _git_output(
        repository, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    )
    expected_stages = _git_output(repository, "ls-files", "--stage", "-z")
    expected_flags = _git_output(repository, "ls-files", "-v", "-z")
    git_state_snapshot = snapshot_git_state(git_client, repository)

    backup_root = _backup_root(tmp_path)
    archive_path = backup_root / "dirty-state"
    snapshot = snapshot_worktree(repository)
    create_archive(
        destination=archive_path,
        worktree_path=repository,
        snapshot=snapshot,
        metadata=_metadata(git_client, repository),
        git=git_client,
        git_state_snapshot=git_state_snapshot,
    )

    shutil.rmtree(repository)
    shutil.rmtree(tmp_path / "origin.git")
    restore_root = _backup_root(tmp_path, "restored-state")
    restored = restore_root / "worktree"
    restore_archive(archive_path=archive_path, destination=restored)

    assert _git_output(restored, "rev-parse", "HEAD") == expected_head
    assert _git_output(
        restored, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    ) == expected_status
    assert _git_output(restored, "ls-files", "--stage", "-z") == expected_stages
    assert _git_output(restored, "ls-files", "-v", "-z") == expected_flags
    assert (restored / "tracked.txt").read_text(encoding="utf-8") == "unstaged\n"
    assert (restored / "operator-note.txt").read_text(encoding="utf-8") == "untracked\n"


def test_restore_archive_populates_readonly_directories_before_final_modes(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    git_client = GitClient(repository)
    readonly = repository / "readonly"
    readonly.mkdir()
    (readonly / "note.txt").write_text("preserved\n", encoding="utf-8")
    git(repository, "add", "readonly/note.txt")
    git(repository, "commit", "-q", "-m", "add readonly directory")
    readonly.chmod(0o500)

    try:
        backup_root = _backup_root(tmp_path)
        archive_path = backup_root / "readonly-directory"
        create_archive(
            destination=archive_path,
            worktree_path=repository,
            snapshot=snapshot_worktree(repository),
            metadata=_metadata(git_client, repository),
            git=git_client,
        )
        restored = _backup_root(tmp_path, "restored-readonly") / "worktree"
        restore_archive(archive_path=archive_path, destination=restored)
    finally:
        readonly.chmod(0o700)

    assert (restored / "readonly" / "note.txt").read_text(encoding="utf-8") == (
        "preserved\n"
    )
    assert stat.S_IMODE((restored / "readonly").lstat().st_mode) == 0o500

def test_dirty_checkpoint_preserves_late_untracked_work_and_rolls_back_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("checkpoint-late")
    worktree = lease.worktree_path
    (worktree / ".gitignore").write_text(
        "node_modules/\n*.env\nruntime/\n", encoding="utf-8"
    )
    git(worktree, "add", ".gitignore")
    git(worktree, "commit", "-q", "-m", "add checkpoint exclusion fixture")
    excluded_dependency = b"excluded checkpoint dependency\n"
    secret = b"preserved ignored checkpoint secret\n"
    runtime = b'{"preserve":"ignored runtime state"}\n'
    (worktree / "node_modules" / "package").mkdir(parents=True)
    (worktree / "node_modules" / "package" / "index.js").write_bytes(
        excluded_dependency
    )
    (worktree / "operator-secret.env").write_bytes(secret)
    (worktree / "runtime").mkdir()
    (worktree / "runtime" / "session.json").write_bytes(runtime)
    (worktree / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(worktree, "add", "tracked.txt")
    (worktree / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (worktree / "captured-untracked.txt").write_text("captured\n", encoding="utf-8")
    expected_head = _git_output(worktree, "rev-parse", "HEAD")
    expected_stages = _git_output(worktree, "ls-files", "--stage", "-z")
    expected_flags = _git_output(worktree, "ls-files", "-v", "-z")
    backup_root = _backup_root(tmp_path)
    token, archive_path = _archive_discard_preview(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        include_uncommitted=True,
        exclude_ignored_paths=("node_modules",),
    )
    original_remove = harness.git.remove_worktree

    def inspect_checkpoint_then_create_late_file(
        path: Path,
        *args: object,
        force: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> None:
        assert not force
        assert env is not None
        checkpoint_tree = _git_output(
            path, "ls-tree", "-r", "-z", "HEAD", environment=env
        )
        assert b"\tnode_modules/package/index.js\0" not in checkpoint_tree
        assert b"\toperator-secret.env\0" in checkpoint_tree
        assert b"\truntime/session.json\0" in checkpoint_tree
        secret_object = _git_output(
            path, "rev-parse", "HEAD:operator-secret.env", environment=env
        ).strip()
        runtime_object = _git_output(
            path, "rev-parse", "HEAD:runtime/session.json", environment=env
        ).strip()
        assert _git_output(
            path,
            "cat-file",
            "blob",
            secret_object.decode("ascii"),
            environment=env,
        ) == secret
        assert _git_output(
            path,
            "cat-file",
            "blob",
            runtime_object.decode("ascii"),
            environment=env,
        ) == runtime
        (path / "late-untracked.txt").write_text("late\n", encoding="utf-8")
        original_remove(path, *args, force=force, env=env)

    monkeypatch.setattr(
        harness.git, "remove_worktree", inspect_checkpoint_then_create_late_file
    )
    result = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=token,
        include_uncommitted=True,
        exclude_ignored_paths=("node_modules",),
    )

    assert result.status == "blocked"
    assert worktree.exists()
    assert _git_output(worktree, "rev-parse", "HEAD") == expected_head
    assert _git_output(worktree, "ls-files", "--stage", "-z") == expected_stages
    assert _git_output(worktree, "ls-files", "-v", "-z") == expected_flags
    assert (worktree / "captured-untracked.txt").read_text(encoding="utf-8") == "captured\n"
    assert (worktree / "late-untracked.txt").read_text(encoding="utf-8") == "late\n"
    assert (worktree / "node_modules" / "package" / "index.js").read_bytes() == (
        excluded_dependency
    )
    assert (worktree / "operator-secret.env").read_bytes() == secret
    assert (worktree / "runtime" / "session.json").read_bytes() == runtime
    archive_snapshot = read_verified_archive(archive_path)["snapshot"]
    archive_names = {entry["name"] for entry in archive_snapshot["entries"]}
    assert archive_snapshot["excluded_paths"] == ["node_modules"]
    assert "node_modules/package/index.js" not in archive_names
    assert "operator-secret.env" in archive_names
    assert "runtime/session.json" in archive_names


def test_dirty_checkpoint_rollback_does_not_publish_untracked_secret_to_common_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("checkpoint-secret")
    worktree = lease.worktree_path
    secret = b"archive checkpoint secret must remain private\n"
    (worktree / "private-note.txt").write_bytes(secret)
    (worktree / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(worktree, "add", "tracked.txt")
    (worktree / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    expected_head = _git_output(worktree, "rev-parse", "HEAD")
    expected_status = _git_output(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    )
    expected_stages = _git_output(worktree, "ls-files", "--stage", "-z")
    secret_object = (
        _git_result(worktree, "hash-object", "--stdin", input_bytes=secret)
        .stdout.decode()
        .strip()
    )
    assert (
        _git_result(worktree, "cat-file", "-e", f"{secret_object}^{{blob}}").returncode
        != 0
    )

    backup_root = _backup_root(tmp_path)
    token, archive_path = _archive_discard_preview(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        include_uncommitted=True,
    )

    def inspect_default_checkpoint_then_fail_remove(
        path: Path,
        *_args: object,
        force: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> None:
        assert not force
        assert env is not None
        checkpoint_tree = _git_output(
            path, "ls-tree", "-r", "-z", "HEAD", environment=env
        )
        assert b"\tprivate-note.txt\0" in checkpoint_tree
        checkpoint_secret = _git_output(
            path, "rev-parse", "HEAD:private-note.txt", environment=env
        ).strip()
        assert _git_output(
            path,
            "cat-file",
            "blob",
            checkpoint_secret.decode("ascii"),
            environment=env,
        ) == secret
        raise GitError("injected worktree removal failure")

    monkeypatch.setattr(
        harness.git, "remove_worktree", inspect_default_checkpoint_then_fail_remove
    )
    result = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=token,
        include_uncommitted=True,
    )

    assert result.status == "blocked"
    assert worktree.exists()
    assert _git_output(worktree, "rev-parse", "HEAD") == expected_head
    assert _git_output(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    ) == expected_status
    assert _git_output(worktree, "ls-files", "--stage", "-z") == expected_stages
    assert (worktree / "private-note.txt").read_bytes() == secret
    assert _git_result(worktree, "cat-file", "-e", f"{secret_object}^{{blob}}").returncode != 0
    assert read_verified_archive(archive_path)["snapshot"]["fingerprint"]


def test_repack_keeps_nonexcluded_content_and_recovers_from_a_failed_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("repack-legacy")
    worktree = lease.worktree_path
    (worktree / ".gitignore").write_text("node_modules/\n*.env\n", encoding="utf-8")
    (worktree / "retained.txt").write_text("retained\n", encoding="utf-8")
    git(worktree, "add", ".gitignore", "retained.txt")
    git(worktree, "commit", "-q", "-m", "add repack fixture")
    expected_head = harness.git.head_sha(worktree)
    (worktree / "node_modules" / "package").mkdir(parents=True)
    (worktree / "node_modules" / "package" / "index.js").write_text(
        "discarded dependency\n", encoding="utf-8"
    )
    (worktree / "operator.env").write_text("retained ignored state\n", encoding="utf-8")

    backup_root = _backup_root(tmp_path)
    archive_token, archive_path = _archive_discard_preview(
        harness, lease_id=lease.id, backup_root=backup_root
    )
    archived = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=archive_token,
    )
    assert archived.status == "ok"
    assert not worktree.exists()
    _rewrite_as_legacy_archive(archive_path)
    assert read_verified_archive(archive_path)["schema_version"] == 1
    original_archive = _archive_file_bytes(archive_path)
    repository_head = harness.git.head_sha(harness.repo)
    repository_refs = _git_output(
        harness.repo, "for-each-ref", "--format=%(refname) %(objectname)"
    )

    repacker = _archive_repacker(harness)
    preview = repacker.run(
        archive_path, exclude_ignored_paths=("node_modules",), apply=False
    )
    assert preview.status == "ok"
    token = _preview_token(preview)

    original_repack = archive_module.repack_archive_contents

    def fail_repack(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ArchiveError("archive_repack_failed", "injected repack failure")

    monkeypatch.setattr(archive_module, "repack_archive_contents", fail_repack)
    failed = repacker.run(
        archive_path,
        exclude_ignored_paths=("node_modules",),
        preview_token=token,
        apply=True,
    )

    assert failed.status == "blocked"
    assert _archive_file_bytes(archive_path) == original_archive
    assert harness.git.head_sha(harness.repo) == repository_head
    assert _git_output(
        harness.repo, "for-each-ref", "--format=%(refname) %(objectname)"
    ) == repository_refs

    monkeypatch.setattr(archive_module, "repack_archive_contents", original_repack)
    repacked = repacker.run(
        archive_path,
        exclude_ignored_paths=("node_modules",),
        preview_token=token,
        apply=True,
    )

    assert repacked.status == "ok"
    assert repacked.decision == "repacked"
    restore_root = _backup_root(tmp_path, "repacked-restore")
    restored = restore_root / "worktree"
    restore_archive(archive_path=archive_path, destination=restored)
    assert git(restored, "rev-parse", "HEAD") == expected_head
    assert (restored / "retained.txt").read_text(encoding="utf-8") == "retained\n"
    assert (restored / "operator.env").read_text(encoding="utf-8") == (
        "retained ignored state\n"
    )
    assert not (restored / "node_modules").exists()
    assert harness.git.head_sha(harness.repo) == repository_head
    assert _git_output(
        harness.repo, "for-each-ref", "--format=%(refname) %(objectname)"
    ) == repository_refs

    completed_archive = _archive_file_bytes(archive_path)
    retried = repacker.run(
        archive_path,
        exclude_ignored_paths=("node_modules",),
        preview_token=token,
        apply=True,
    )
    assert retried.status == "ok"
    assert retried.decision == "repacked"
    assert _archive_file_bytes(archive_path) == completed_archive


def test_repack_restores_original_when_staging_disappears_after_source_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("repack-staging-loss")
    worktree = lease.worktree_path
    (worktree / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(worktree, "add", ".gitignore")
    git(worktree, "commit", "-q", "-m", "ignore dependencies")
    (worktree / "node_modules" / "package").mkdir(parents=True)
    (worktree / "node_modules" / "package" / "index.js").write_text(
        "must recover\n", encoding="utf-8"
    )

    backup_root = _backup_root(tmp_path)
    archive_token, archive_path = _archive_discard_preview(
        harness, lease_id=lease.id, backup_root=backup_root
    )
    archived = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=archive_token,
    )
    assert archived.status == "ok"
    original_archive = _archive_file_bytes(archive_path)
    repacker = _archive_repacker(harness)
    preview = repacker.run(
        archive_path, exclude_ignored_paths=("node_modules",), apply=False
    )
    assert preview.status == "ok"
    token = _preview_token(preview)
    original_rename = repacker._rename

    def lose_staging_before_publish(source: Path, destination: Path) -> None:
        if source != archive_path and destination == archive_path:
            shutil.rmtree(source)
            raise ArchiveError(
                "archive_repack_failed", "injected staging disappearance"
            )
        original_rename(source, destination)

    monkeypatch.setattr(repacker, "_rename", lose_staging_before_publish)
    interrupted = repacker.run(
        archive_path,
        exclude_ignored_paths=("node_modules",),
        preview_token=token,
        apply=True,
    )

    assert interrupted.status == "blocked"
    journal_path = archive_path.parent / f".awf-repack-{archive_path.name}.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert not archive_path.exists()
    assert (archive_path.parent / journal["previous"]).is_dir()
    assert not (archive_path.parent / journal["staging"]).exists()

    recovered = _archive_repacker(harness).run(
        archive_path,
        exclude_ignored_paths=("node_modules",),
        preview_token=token,
        apply=True,
    )

    assert recovered.status == "blocked"
    assert recovered.blockers[0]["code"] == "published_archive_invalid"
    assert _archive_file_bytes(archive_path) == original_archive
    assert not journal_path.exists()
    restored = _backup_root(tmp_path, "recovered-original") / "worktree"
    restore_archive(archive_path=archive_path, destination=restored)
    assert (restored / "node_modules" / "package" / "index.js").read_text(
        encoding="utf-8"
    ) == "must recover\n"

@pytest.mark.skipif(not hasattr(os, "fork"), reason="process crash boundary needs fork")
def test_interrupted_checkpoint_recovers_then_failed_removal_requires_fresh_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("checkpoint-interrupt")
    worktree = lease.worktree_path
    (worktree / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(worktree, "add", "tracked.txt")
    (worktree / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (worktree / "captured-untracked.txt").write_text("captured\n", encoding="utf-8")
    expected_head = _git_output(worktree, "rev-parse", "HEAD")
    expected_status = _git_output(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    )
    expected_stages = _git_output(worktree, "ls-files", "--stage", "-z")
    backup_root = _backup_root(tmp_path)
    token, archive_path = _archive_discard_preview(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        include_uncommitted=True,
    )

    child = os.fork()
    if child == 0:
        checkpoint = archive_discard_module.archived_removal_checkpoint

        def terminate_after_checkpoint(
            *args: object, **kwargs: object
        ) -> object:
            entered = checkpoint(*args, **kwargs)
            entered.__enter__()
            os._exit(73)

        archive_discard_module.archived_removal_checkpoint = terminate_after_checkpoint
        _archive_discard_apply(
            harness,
            lease_id=lease.id,
            backup_root=backup_root,
            preview_token=token,
            include_uncommitted=True,
        )
        os._exit(74)

    _, child_status = os.waitpid(child, 0)
    assert os.WIFEXITED(child_status)
    assert os.WEXITSTATUS(child_status) == 73
    assert (archive_path / "manifest.json").is_file()
    recovery_service = _fresh_archive_service(harness)
    original_remove = recovery_service.git.remove_worktree

    def fail_remove(*_args: object, **_kwargs: object) -> None:
        raise GitError("injected removal failure after checkpoint recovery")

    monkeypatch.setattr(recovery_service.git, "remove_worktree", fail_remove)
    recovered = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=token,
        include_uncommitted=True,
        service=recovery_service,
    )

    assert recovered.status == "blocked"
    assert worktree.exists()
    assert _git_output(worktree, "rev-parse", "HEAD") == expected_head
    assert _git_output(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    ) == expected_status
    assert _git_output(worktree, "ls-files", "--stage", "-z") == expected_stages
    assert (worktree / "captured-untracked.txt").read_text(encoding="utf-8") == (
        "captured\n"
    )

    monkeypatch.setattr(recovery_service.git, "remove_worktree", original_remove)
    stale = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=token,
        include_uncommitted=True,
        service=recovery_service,
    )
    assert stale.status == "blocked"
    assert any(
        blocker["code"] == "preview_token_mismatch" for blocker in stale.blockers
    )
    fresh_token, _ = _archive_discard_preview(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        include_uncommitted=True,
    )
    resumed = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=fresh_token,
        include_uncommitted=True,
        service=recovery_service,
    )
    assert resumed.status == "ok"
    assert not worktree.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="process crash boundary needs fork")
def test_interrupted_checkpoint_preserves_uncommitted_work_when_external_index_drifts(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("checkpoint-external-drift")
    worktree = lease.worktree_path
    (worktree / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(worktree, "add", "tracked.txt")
    required_object = git(worktree, "rev-parse", "HEAD:README.txt")
    (worktree / "captured-untracked.txt").write_text("captured\n", encoding="utf-8")
    backup_root = _backup_root(tmp_path)
    token, archive_path = _archive_discard_preview(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        include_uncommitted=True,
    )

    child = os.fork()
    if child == 0:
        checkpoint = archive_discard_module.archived_removal_checkpoint

        def terminate_after_checkpoint(
            *args: object, **kwargs: object
        ) -> object:
            entered = checkpoint(*args, **kwargs)
            entered.__enter__()
            os._exit(73)

        archive_discard_module.archived_removal_checkpoint = terminate_after_checkpoint
        _archive_discard_apply(
            harness,
            lease_id=lease.id,
            backup_root=backup_root,
            preview_token=token,
            include_uncommitted=True,
        )
        os._exit(74)

    _, child_status = os.waitpid(child, 0)
    assert os.WIFEXITED(child_status)
    assert os.WEXITSTATUS(child_status) == 73
    recovery_service = _fresh_archive_service(harness)
    (worktree / "external-after-checkpoint.txt").write_text(
        "must remain staged\n", encoding="utf-8"
    )
    git(worktree, "add", "external-after-checkpoint.txt")
    blocked = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=token,
        include_uncommitted=True,
        service=recovery_service,
    )

    assert blocked.status == "blocked"
    assert worktree.exists()
    assert (worktree / "external-after-checkpoint.txt").read_text(
        encoding="utf-8"
    ) == "must remain staged\n"
    assert b"\texternal-after-checkpoint.txt\0" in _git_output(
        worktree, "ls-files", "--stage", "-z"
    )
    restore_root = _backup_root(tmp_path, "external-drift-restore")
    restored = restore_root / "worktree"
    restore_archive(archive_path=archive_path, destination=restored)
    assert (restored / "captured-untracked.txt").read_text(encoding="utf-8") == (
        "captured\n"
    )
    assert _git_result(
        restored, "cat-file", "-e", f"{required_object}^{{blob}}"
    ).returncode == 0


def test_absent_dirty_worktree_resumes_its_reservation_without_a_checkpoint_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("checkpoint-absent")
    worktree = lease.worktree_path
    (worktree / "captured-untracked.txt").write_text("captured\n", encoding="utf-8")
    backup_root = _backup_root(tmp_path)
    token, archive_path = _archive_discard_preview(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        include_uncommitted=True,
    )
    original_complete = harness.registry.complete_cleanup

    def fail_completion(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected cleanup completion failure")

    monkeypatch.setattr(harness.registry, "complete_cleanup", fail_completion)
    interrupted = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=token,
        include_uncommitted=True,
    )

    assert interrupted.status == "blocked"
    assert not worktree.exists()
    assert read_verified_archive(archive_path)["snapshot"]["fingerprint"]

    monkeypatch.setattr(harness.registry, "complete_cleanup", original_complete)
    resumed = _archive_discard_apply(
        harness,
        lease_id=lease.id,
        backup_root=backup_root,
        preview_token=token,
        include_uncommitted=True,
        service=_fresh_archive_service(harness),
    )
    assert resumed.status == "ok"
    assert not worktree.exists()


@pytest.mark.parametrize(("parent_mode", "accepted"), ((0o1777, True), (0o777, False), (0o775, False)))
def test_backup_root_accepts_only_sticky_shared_ancestors(
    tmp_path: Path, parent_mode: int, accepted: bool
) -> None:
    shared = tmp_path.resolve() / "shared"
    shared.mkdir()
    root = shared / "backups"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    shared.chmod(parent_mode)
    try:
        if accepted:
            assert validate_backup_root(root, forbidden_roots=()) == root
        else:
            with pytest.raises(ArchiveError) as raised:
                validate_backup_root(root, forbidden_roots=())
            assert raised.value.code == "backup_root_unsafe"
    finally:
        shared.chmod(0o700)
