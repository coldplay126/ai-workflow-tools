"""Behavioral contracts for shared Supervisor runtime paths."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

import pytest

from awf.supervisor.runtime_paths import RuntimePathError, resolve_runtime_paths


def test_local_defaults_share_one_state_root_and_derived_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo_root = home / "Documents" / "GitHub"
    repo_root.mkdir(parents=True)

    paths = resolve_runtime_paths(
        environment="local", environ={}, home=home
    )

    assert paths.state_root == home / "Library" / "Application Support" / "AWF" / "supervisor"
    assert paths.store_path == paths.state_root / "supervisor.db"
    assert paths.active_lease_path == paths.state_root / "active-lease.json"
    assert paths.repo_root == repo_root.resolve()
    assert paths.state_root.is_dir()


def test_aws_paths_use_independent_explicit_environment_overrides(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    lease_path = tmp_path / "lease" / "active.json"
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    environ: Dict[str, str] = {
        "AWF_SUPERVISOR_STATE_DIR": str(tmp_path / "ignored-state"),
        "AWF_SUPERVISOR_ACTIVE_LEASE_PATH": str(tmp_path / "ignored-lease.json"),
        "AWF_SUPERVISOR_REPO_ROOT": str(tmp_path / "ignored-repos"),
    }

    paths = resolve_runtime_paths(
        environment="aws",
        state_dir=state_root,
        active_lease_path=lease_path,
        repo_root=repo_root,
        environ=environ,
    )

    assert paths.state_root == state_root.resolve()
    assert paths.store_path == state_root.resolve() / "supervisor.db"
    assert paths.active_lease_path == lease_path.resolve()
    assert paths.repo_root == repo_root.resolve()
    assert paths.active_lease_path.parent.is_dir()


def test_environment_overrides_apply_without_cross_path_coupling(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    lease_path = tmp_path / "leases" / "active.json"
    repo_root = tmp_path / "repos"
    repo_root.mkdir()

    paths = resolve_runtime_paths(
        environment="aws",
        environ={
            "AWF_SUPERVISOR_STATE_DIR": str(state_root),
            "AWF_SUPERVISOR_ACTIVE_LEASE_PATH": str(lease_path),
            "AWF_SUPERVISOR_REPO_ROOT": str(repo_root),
        },
    )

    assert paths.state_root == state_root.resolve()
    assert paths.store_path == state_root.resolve() / "supervisor.db"
    assert paths.active_lease_path == lease_path.resolve()
    assert paths.repo_root == repo_root.resolve()


@pytest.mark.parametrize("field", ["state_dir", "active_lease_path", "repo_root"])
def test_relative_runtime_paths_are_rejected(field: str, tmp_path: Path) -> None:
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    kwargs = {
        "state_dir": tmp_path / "state",
        "active_lease_path": tmp_path / "active.json",
        "repo_root": repo_root,
    }
    kwargs[field] = Path("relative")

    with pytest.raises(RuntimePathError, match="absolute"):
        resolve_runtime_paths(environment="local", environ={}, **kwargs)


def test_non_directory_repository_root_and_state_root_are_rejected(tmp_path: Path) -> None:
    state_file = tmp_path / "state-file"
    repo_file = tmp_path / "repo-file"
    state_file.write_text("not a directory", encoding="utf-8")
    repo_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimePathError, match="state root"):
        resolve_runtime_paths(
            environment="local",
            state_dir=state_file,
            repo_root=tmp_path,
            environ={},
        )
    with pytest.raises(RuntimePathError, match="repository root"):
        resolve_runtime_paths(
            environment="local",
            state_dir=tmp_path / "state",
            repo_root=repo_file,
            environ={},
        )


def test_marker_parent_that_cannot_be_a_private_directory_is_rejected(
    tmp_path: Path,
) -> None:
    marker_parent = tmp_path / "marker-parent"
    marker_parent.write_text("not a directory", encoding="utf-8")
    repo_root = tmp_path / "repos"
    repo_root.mkdir()

    with pytest.raises(RuntimePathError, match="active lease parent"):
        resolve_runtime_paths(
            environment="local",
            state_dir=tmp_path / "state",
            active_lease_path=marker_parent / "active-lease.json",
            repo_root=repo_root,
            environ={},
        )


def test_repository_root_is_canonicalized_not_an_untrusted_child(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escaping").symlink_to(outside, target_is_directory=True)

    paths = resolve_runtime_paths(
        environment="local",
        state_dir=tmp_path / "state",
        repo_root=root,
        environ={},
    )

    assert paths.repo_root == root.resolve()
    assert os.path.commonpath((str(paths.repo_root), str((root / "escaping").resolve()))) != str(paths.repo_root)
