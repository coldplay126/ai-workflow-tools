"""Real-Git contract tests for Supervisor workspace adapters."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import pytest

from awf.supervisor.client import RepoRef
from awf.supervisor.workspace import (
    AgentctlWorkspaceAdapter,
    LocalGitWorkspaceAdapter,
    WorkspaceConflict,
    WorkspaceError,
    WorkspaceRecoveryError,
    WorkspaceValidationError,
)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _git_global(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def git_branch(path: Path) -> str:
    return _git(path, "branch", "--show-current")


def git_rev_parse(path: Path, ref: str) -> str:
    return _git(path, "rev-parse", "--verify", ref)


def create_remote_clone(github_root: Path, name: str, branch: str) -> Path:
    """Create a normal canonical clone with one pushed origin branch."""
    origin = github_root.parent / "origins" / "{}.git".format(name)
    bootstrap = github_root.parent / "bootstrap-{}".format(name)
    origin.parent.mkdir(parents=True, exist_ok=True)
    github_root.mkdir(parents=True, exist_ok=True)
    _git_global(origin.parent, "init", "--bare", str(origin))
    _git_global(bootstrap.parent, "init", "-b", branch, str(bootstrap))
    _git(bootstrap, "config", "user.email", "workspace-test@example.invalid")
    _git(bootstrap, "config", "user.name", "Workspace Test")
    (bootstrap / "README.txt").write_text("seed\n", encoding="utf-8")
    _git(bootstrap, "add", "README.txt")
    _git(bootstrap, "commit", "-m", "seed")
    _git(bootstrap, "remote", "add", "origin", str(origin))
    _git(bootstrap, "push", "-u", "origin", branch)
    _git_global(
        origin.parent,
        "--git-dir",
        str(origin),
        "symbolic-ref",
        "HEAD",
        "refs/heads/{}".format(branch),
    )
    _git_global(github_root, "clone", str(origin), name)
    canonical = github_root / name
    _git(canonical, "config", "user.email", "workspace-test@example.invalid")
    _git(canonical, "config", "user.name", "Workspace Test")
    return canonical


def create_origin_branch_with_commit(canonical: Path, branch: str, content: str) -> str:
    _git(canonical, "switch", "-c", branch, "origin/main")
    (canonical / "README.txt").write_text(content, encoding="utf-8")
    _git(canonical, "add", "README.txt")
    _git(canonical, "commit", "-m", "{} commit".format(branch))
    _git(canonical, "push", "-u", "origin", branch)
    _git(canonical, "switch", "main")
    _git(canonical, "fetch", "--prune", "origin", branch)
    return git_rev_parse(canonical, "refs/remotes/origin/{}".format(branch))


def _manifest(prepared_path: Path) -> Dict[str, Any]:
    return json.loads(prepared_path.read_text(encoding="utf-8"))


def _recovery_checkpoint(
    prepared_manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    agent_id: str = "local-1",
    environment: str = "local",
    cross_node_eligible: bool = False,
) -> Dict[str, Any]:
    repos = []
    for repository in prepared_manifest["repositories"]:
        repos.append(
            {
                "repo": repository["repo"],
                "base": repository["base"],
                "head": repository["commit"],
                "remote_ref": repository["remote_ref"],
                "clean": True,
                "pushed": True,
            }
        )
    return {
        "schema_version": 1,
        "kind": "awf-supervisor-recovery-checkpoint",
        "job_id": prepared_manifest["job_id"],
        "generation": prepared_manifest["generation"],
        "origin_agent_id": agent_id,
        "origin_environment": environment,
        "native": {
            "batch_fingerprint": "a" * 64,
            "state": "resuming",
            "coordinator_session_id": "session-1",
        },
        "worker_descriptors": [{"name": "SupervisorJob", "sha256": "b" * 64}],
        "handles": {
            "task_id": "task-1",
            "agent_uri": "agent://agent-1",
            "history_uri": "history://history-1",
        },
        "workspace_manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "repos": repos,
        "cross_node_eligible": cross_node_eligible,
    }


def _unsafe_repo_ref(repo: str, base: str) -> RepoRef:
    """Build an intentionally invalid wire object to test adapter-side validation."""
    reference = object.__new__(RepoRef)
    object.__setattr__(reference, "repo", repo)
    object.__setattr__(reference, "base", base)
    return reference


def test_local_workspace_creates_one_isolated_worktree_per_repo(tmp_path: Path) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    create_remote_clone(github_root, "web", "develop")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")

    prepared = adapter.prepare(
        job_id="job-1",
        generation=2,
        repo_refs=(RepoRef("api", "main"), RepoRef("web", "develop")),
    )

    expected_branch = "awf/supervisor-" + sha256(b"job-1\n2").hexdigest()[:20]
    assert prepared.cwd == tmp_path / "state/jobs/job-1/g2/workspace"
    assert git_branch(prepared.cwd / "api") == expected_branch
    assert git_branch(prepared.cwd / "web") == expected_branch
    assert prepared.repo_paths == (prepared.cwd / "api", prepared.cwd / "web")
    assert (prepared.cwd / "AGENTS.md").is_file()
    instructions = (prepared.cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert "api" in instructions and "main" in instructions and expected_branch in instructions
    assert "web" in instructions and "develop" in instructions
    assert prepared.manifest_path.is_file()


def test_local_workspace_uses_requested_origin_base_not_canonical_head(tmp_path: Path) -> None:
    github_root = tmp_path / "repos"
    canonical = create_remote_clone(github_root, "api", "main")
    main_commit = git_rev_parse(canonical, "HEAD")
    develop_commit = create_origin_branch_with_commit(canonical, "develop", "develop-only\n")
    assert develop_commit != main_commit

    prepared = LocalGitWorkspaceAdapter(
        github_root=github_root, state_root=tmp_path / "state"
    ).prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "develop"),))

    assert git_rev_parse(prepared.cwd / "api", "HEAD") == git_rev_parse(
        canonical, "refs/remotes/origin/develop"
    )
    assert git_rev_parse(prepared.cwd / "api", "HEAD") != main_commit


@pytest.mark.parametrize(
    "repo_ref",
    (
        _unsafe_repo_ref("../api", "main"),
        _unsafe_repo_ref("api", "main..evil"),
    ),
)
def test_local_workspace_rejects_traversal_and_invalid_refs_before_use(
    tmp_path: Path, repo_ref: RepoRef
) -> None:
    adapter = LocalGitWorkspaceAdapter(github_root=tmp_path / "repos", state_root=tmp_path / "state")

    with pytest.raises(WorkspaceValidationError):
        adapter.prepare(job_id="job-1", generation=1, repo_refs=(repo_ref,))

    assert not (tmp_path / "state").exists()


def test_local_workspace_rejects_missing_dirty_and_nonexistent_origin_bases(tmp_path: Path) -> None:
    github_root = tmp_path / "repos"
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")

    with pytest.raises(WorkspaceError):
        adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("missing", "main"),))

    canonical = create_remote_clone(github_root, "api", "main")
    (canonical / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),))
    (canonical / "dirty.txt").unlink()

    with pytest.raises(WorkspaceError):
        adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "does-not-exist"),))


def test_local_workspace_rejects_existing_branch_not_owned_by_its_manifest(tmp_path: Path) -> None:
    github_root = tmp_path / "repos"
    canonical = create_remote_clone(github_root, "api", "main")
    branch = "awf/supervisor-" + sha256(b"job-1\n1").hexdigest()[:20]
    _git(canonical, "branch", branch, "origin/main")

    with pytest.raises(WorkspaceConflict):
        LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state").prepare(
            job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),)
        )


def test_local_workspace_is_idempotent_per_generation_and_isolates_next_generation(
    tmp_path: Path,
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")

    first = adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),))
    repeated = adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),))
    next_generation = adapter.prepare(
        job_id="job-1", generation=2, repo_refs=(RepoRef("api", "main"),)
    )

    assert repeated == first
    assert next_generation.cwd != first.cwd
    assert git_branch(next_generation.cwd / "api") != git_branch(first.cwd / "api")


def test_local_workspace_refuses_generation_directory_symlink(tmp_path: Path) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    task_parent = tmp_path / "state/jobs/job-1"
    task_parent.mkdir(parents=True)
    (task_parent / "g1").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(WorkspaceConflict):
        LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state").prepare(
            job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),)
        )


def test_local_cleanup_refuses_dirty_or_unpushed_worktree_without_mutation(tmp_path: Path) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")

    dirty = adapter.prepare(job_id="dirty", generation=1, repo_refs=(RepoRef("api", "main"),))
    (dirty.cwd / "api" / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    assert adapter.cleanup(dirty) is False
    assert (dirty.cwd / "api").is_dir()

    unpushed = adapter.prepare(job_id="unpushed", generation=1, repo_refs=(RepoRef("api", "main"),))
    (unpushed.cwd / "api" / "commit.txt").write_text("commit\n", encoding="utf-8")
    _git(unpushed.cwd / "api", "add", "commit.txt")
    _git(unpushed.cwd / "api", "commit", "-m", "unpushed work")
    assert adapter.cleanup(unpushed) is False
    assert (unpushed.cwd / "api").is_dir()


def test_local_retained_native_recovery_requires_matching_identity_and_manifest_digest(
    tmp_path: Path,
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),))
    checkpoint = _recovery_checkpoint(_manifest(prepared.manifest_path), prepared.manifest_path)

    recovered = adapter.recover(
        job_id="job-1",
        generation=2,
        repo_refs=(RepoRef("api", "main"),),
        checkpoint=checkpoint,
        current_agent_id="local-1",
        current_environment="local",
    )
    assert recovered.resume_native is True
    assert recovered.prepared == prepared
    identity_checkpoint = _recovery_checkpoint(
        _manifest(prepared.manifest_path), prepared.manifest_path
    )
    identity_checkpoint["repos"][0]["head"] = "0" * 40
    with pytest.raises(WorkspaceRecoveryError):
        adapter.recover(
            job_id="job-1",
            generation=2,
            repo_refs=(RepoRef("api", "main"),),
            checkpoint=identity_checkpoint,
            current_agent_id="local-1",
            current_environment="local",
        )


    checkpoint["workspace_manifest_sha256"] = "0" * 64
    with pytest.raises(WorkspaceRecoveryError):
        adapter.recover(
            job_id="job-1",
            generation=2,
            repo_refs=(RepoRef("api", "main"),),
            checkpoint=checkpoint,
            current_agent_id="local-1",
            current_environment="local",
        )


def test_local_to_aws_recovery_creates_current_generation_from_immutable_commits(
    tmp_path: Path,
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    prior = adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),))
    prior_manifest = _manifest(prior.manifest_path)
    checkpoint = _recovery_checkpoint(
        prior_manifest,
        prior.manifest_path,
        agent_id="local-1",
        environment="local",
        cross_node_eligible=True,
    )

    recovered = adapter.recover(
        job_id="job-1",
        generation=2,
        repo_refs=(RepoRef("api", "main"),),
        checkpoint=checkpoint,
        current_agent_id="aws-1",
        current_environment="aws",
    )

    assert recovered.resume_native is False
    assert recovered.prepared.cwd == tmp_path / "state/jobs/job-1/g2/workspace"
    assert git_rev_parse(recovered.prepared.cwd / "api", "HEAD") == prior_manifest["repositories"][0]["commit"]


def test_local_cross_node_recovery_rejects_reordered_repository_references(
    tmp_path: Path,
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    create_remote_clone(github_root, "web", "develop")
    refs = (RepoRef("api", "main"), RepoRef("web", "develop"))
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    prior = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)
    checkpoint = _recovery_checkpoint(
        _manifest(prior.manifest_path),
        prior.manifest_path,
        agent_id="aws-1",
        environment="aws",
        cross_node_eligible=True,
    )
    checkpoint["repos"].reverse()

    with pytest.raises(WorkspaceRecoveryError):
        adapter.recover(
            job_id="job-1",
            generation=2,
            repo_refs=refs,
            checkpoint=checkpoint,
            current_agent_id="local-1",
            current_environment="local",
        )


@pytest.mark.parametrize(
    ("canonical", "legacy"),
    (
        ("origin_agent_id", "agent_id"),
        ("origin_environment", "environment"),
        ("native", "native_checkpoint"),
        ("repos", "repositories"),
    ),
)
def test_local_recovery_rejects_each_legacy_checkpoint_alias(
    tmp_path: Path, canonical: str, legacy: str
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=(RepoRef("api", "main"),))
    checkpoint = _recovery_checkpoint(_manifest(prepared.manifest_path), prepared.manifest_path)
    checkpoint[legacy] = checkpoint.pop(canonical)

    with pytest.raises(WorkspaceRecoveryError):
        adapter.recover(
            job_id="job-1",
            generation=2,
            repo_refs=(RepoRef("api", "main"),),
            checkpoint=checkpoint,
            current_agent_id="local-1",
            current_environment="local",
        )

def _write_fake_agentctl(tmp_path: Path) -> Path:
    executable = tmp_path / "agentctl"
    executable.write_text(
        "#!{}\n"
        "import json, os, sys\n"
        "with open(os.environ['AGENTCTL_LOG'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'task-create' and os.environ.get('AGENTCTL_EXISTING') == '1':\n"
        "    raise SystemExit(2)\n"
        "if sys.argv[1] == 'task-path':\n"
        "    print(os.environ['AGENTCTL_TASK_PATH'])\n"
        "elif sys.argv[1] == 'task-status':\n"
        "    print(os.environ['AGENTCTL_STATUS'])\n".format(os.sys.executable),
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _agentctl_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    task_path: Path,
    existing: bool = False,
    status: str = "{}",
) -> None:
    log = tmp_path / "agentctl.log"
    monkeypatch.setenv("AGENTCTL_LOG", str(log))
    monkeypatch.setenv("AGENTCTL_TASK_PATH", str(task_path))
    monkeypatch.setenv("AGENTCTL_STATUS", status)
    if existing:
        monkeypatch.setenv("AGENTCTL_EXISTING", "1")


def _agentctl_roots(tmp_path: Path) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace"
    repo_root = workspace_root / "repos"
    repo_root.mkdir(parents=True)
    return repo_root, workspace_root


def _agentctl_task_name(job_id: str = "job-1", generation: int = 1) -> str:
    return "awf-" + sha256("{}\n{}".format(job_id, generation).encode()).hexdigest()[:20]


def _agentctl_task_status(
    task_name: str, workspace_root: Path, refs: tuple[RepoRef, ...]
) -> str:
    return json.dumps(
        {
            "task_name": task_name,
            "repositories": [
                {
                    "repo": ref.repo,
                    "base": ref.base,
                    "worktree_path": str(
                        workspace_root / "worktrees" / ref.repo / task_name
                    ),
                }
                for ref in refs
            ],
        }
    )


def _create_agentctl_task_layout(
    repo_root: Path, workspace_root: Path, task_name: str, refs: tuple[RepoRef, ...]
) -> Path:
    task_path = workspace_root / "tasks" / task_name
    task_path.mkdir(parents=True)
    for ref in refs:
        (repo_root / ref.repo).mkdir()
        (workspace_root / "worktrees" / ref.repo / task_name).mkdir(parents=True)
    return task_path


def test_agentctl_adapter_accepts_only_configured_task_and_worktree_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("repo-a", "main"), RepoRef("repo-b", "develop"))
    task_name = _agentctl_task_name()
    task_path = _create_agentctl_task_layout(repo_root, workspace_root, task_name, refs)
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )

    prepared = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    ).prepare(job_id="job-1", generation=1, repo_refs=refs)

    assert prepared.cwd == task_path
    assert prepared.manifest_path == task_path / ".awf-supervisor-workspace.json"
    assert prepared.repo_paths == (
        workspace_root / "worktrees" / "repo-a" / task_name,
        workspace_root / "worktrees" / "repo-b" / task_name,
    )


@pytest.mark.parametrize("task_path_value", ("outside", "parent-traversal"))
def test_agentctl_adapter_rejects_unsafe_task_output_without_writing_or_removing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_path_value: str
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    (repo_root / "api").mkdir()
    if task_path_value == "outside":
        task_path = tmp_path / "outside" / task_name
    else:
        task_path = workspace_root / "tasks" / "nested" / ".." / task_name
    task_path.mkdir(parents=True)
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )

    with pytest.raises(WorkspaceError):
        AgentctlWorkspaceAdapter(
            agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
        ).prepare(job_id="job-1", generation=1, repo_refs=refs)

    assert not (task_path / ".awf-supervisor-workspace.json").exists()
    assert "task-remove" not in (tmp_path / "agentctl.log").read_text(encoding="utf-8")


def test_agentctl_adapter_rejects_intermediate_task_symlink_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    (repo_root / "api").mkdir()
    outside_tasks = tmp_path / "outside-tasks"
    task_path = outside_tasks / task_name
    task_path.mkdir(parents=True)
    (workspace_root / "tasks").symlink_to(outside_tasks, target_is_directory=True)
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=workspace_root / "tasks" / task_name,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )

    with pytest.raises(WorkspaceError):
        AgentctlWorkspaceAdapter(
            agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
        ).prepare(job_id="job-1", generation=1, repo_refs=refs)

    assert not (task_path / ".awf-supervisor-workspace.json").exists()
    assert "task-remove" not in (tmp_path / "agentctl.log").read_text(encoding="utf-8")


def test_agentctl_adapter_rejects_escaped_status_worktree_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    task_path = _create_agentctl_task_layout(repo_root, workspace_root, task_name, refs)
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=json.dumps(
            {
                "task_name": task_name,
                "repositories": [
                    {
                        "repo": "api",
                        "base": "main",
                        "worktree_path": str(
                            workspace_root
                            / "worktrees"
                            / "api"
                            / ".."
                            / "api"
                            / task_name
                        ),
                    }
                ],
            }
        ),
    )

    with pytest.raises(WorkspaceConflict):
        AgentctlWorkspaceAdapter(
            agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
        ).prepare(job_id="job-1", generation=1, repo_refs=refs)

    assert not (task_path / ".awf-supervisor-workspace.json").exists()


def test_agentctl_cleanup_refuses_escaped_manifest_without_removing_unowned_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    task_path = _create_agentctl_task_layout(repo_root, workspace_root, task_name, refs)
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )
    adapter = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    )
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped_manifest = outside / prepared.manifest_path.name
    escaped_manifest.write_text(
        prepared.manifest_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    escaped = type(prepared)(
        cwd=outside,
        manifest_path=escaped_manifest,
        repo_paths=(outside / "api",),
        cleanup_token=prepared.cleanup_token,
    )

    assert adapter.cleanup(escaped) is False
    assert escaped_manifest.is_file()
    assert "task-remove" not in (tmp_path / "agentctl.log").read_text(encoding="utf-8")

def test_agentctl_adapter_uses_deterministic_task_name_and_exact_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("repo-a", "main"), RepoRef("repo-b", "develop"))
    task_name = "awf-0123456789abcdefabcd"
    task_path = _create_agentctl_task_layout(repo_root, workspace_root, task_name, refs)
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )
    monkeypatch.setattr(
        "awf.supervisor.workspace.sha256",
        lambda _value: type(
            "Digest",
            (),
            {"hexdigest": lambda self: "0123456789abcdefabcd" + "e" * 44},
        )(),
    )
    adapter = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    )

    prepared = adapter.prepare(job_id="job-1", generation=2, repo_refs=refs)

    assert prepared.cwd == task_path
    assert prepared.cleanup_token == task_name
    assert [
        json.loads(line)
        for line in (tmp_path / "agentctl.log").read_text(encoding="utf-8").splitlines()
    ] == [
        ["task-create", task_name, "repo-a:main", "repo-b:develop"],
        ["task-path", task_name],
        ["task-status", task_name],
    ]


def test_agentctl_adapter_requires_status_proof_before_reusing_existing_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("repo-a", "main"),)
    task_name = _agentctl_task_name(generation=2)
    task_path = _create_agentctl_task_layout(repo_root, workspace_root, task_name, refs)
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        existing=True,
        status=json.dumps(
            {
                "task_name": task_name,
                "repositories": [{"repo": "other", "base": "main"}],
            }
        ),
    )
    adapter = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    )

    with pytest.raises(WorkspaceConflict):
        adapter.prepare(job_id="job-1", generation=2, repo_refs=refs)

    assert [
        json.loads(line)
        for line in (tmp_path / "agentctl.log").read_text(encoding="utf-8").splitlines()
    ] == [
        ["task-create", task_name, "repo-a:main"],
        ["task-path", task_name],
        ["task-status", task_name],
    ]

def test_agentctl_retained_native_recovery_accepts_canonical_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    task_path = workspace_root / "tasks" / task_name
    task_path.mkdir(parents=True)
    create_remote_clone(repo_root, "api", "main")
    create_remote_clone(workspace_root / "worktrees" / "api", task_name, "main")
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )
    adapter = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    )
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)
    checkpoint = {
        "schema_version": 1,
        "kind": "awf-supervisor-recovery-checkpoint",
        "job_id": "job-1",
        "generation": 1,
        "origin_agent_id": "local-1",
        "origin_environment": "local",
        "native": {
            "batch_fingerprint": "a" * 64,
            "state": "resuming",
            "coordinator_session_id": "session-1",
        },
        "worker_descriptors": [{"name": "SupervisorJob", "sha256": "b" * 64}],
        "handles": {
            "task_id": "task-1",
            "agent_uri": "agent://agent-1",
            "history_uri": "history://history-1",
        },
        "workspace_manifest_sha256": sha256(prepared.manifest_path.read_bytes()).hexdigest(),
        "repos": adapter.checkpoint_repositories(prepared, refs),
        "cross_node_eligible": True,
    }

    recovered = adapter.recover(
        job_id="job-1",
        generation=2,
        repo_refs=refs,
        checkpoint=checkpoint,
        current_agent_id="local-1",
        current_environment="local",
    )

    assert recovered.prepared == prepared
    assert recovered.resume_native is True

def test_local_checkpoint_repositories_attest_clean_pushed_multi_repo_state(
    tmp_path: Path,
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    create_remote_clone(github_root, "web", "develop")
    refs = (RepoRef("api", "main"), RepoRef("web", "develop"))
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)

    rows = adapter.checkpoint_repositories(prepared, refs)

    assert rows == [
        {
            "repo": "api",
            "base": "main",
            "head": git_rev_parse(prepared.cwd / "api", "HEAD"),
            "remote_ref": "refs/heads/main",
            "clean": True,
            "pushed": True,
        },
        {
            "repo": "web",
            "base": "develop",
            "head": git_rev_parse(prepared.cwd / "web", "HEAD"),
            "remote_ref": "refs/heads/develop",
            "clean": True,
            "pushed": True,
        },
    ]


@pytest.mark.parametrize("kind", ("dirty", "untracked", "unpushed"))
def test_local_checkpoint_repositories_fails_closed_for_local_source_state(
    tmp_path: Path, kind: str
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    refs = (RepoRef("api", "main"),)
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)
    repository = prepared.cwd / "api"
    if kind == "dirty":
        (repository / "README.txt").write_text("changed\n", encoding="utf-8")
    elif kind == "untracked":
        (repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    else:
        (repository / "commit.txt").write_text("commit\n", encoding="utf-8")
        _git(repository, "add", "commit.txt")
        _git(repository, "commit", "-m", "unpushed")

    assert adapter.checkpoint_repositories(prepared, refs) == [
        {
            "repo": "api",
            "base": "main",
            "head": git_rev_parse(repository, "HEAD"),
            "remote_ref": "refs/heads/main",
            "clean": False if kind in ("dirty", "untracked") else True,
            "pushed": False,
        }
    ]


def test_local_checkpoint_repositories_fails_closed_when_remote_tracking_ref_changes(
    tmp_path: Path,
) -> None:
    github_root = tmp_path / "repos"
    canonical = create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    refs = (RepoRef("api", "main"),)
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)
    (canonical / "advanced.txt").write_text("advanced\n", encoding="utf-8")
    _git(canonical, "add", "advanced.txt")
    _git(canonical, "commit", "-m", "advance origin")
    _git(canonical, "update-ref", "refs/remotes/origin/main", "HEAD")

    rows = adapter.checkpoint_repositories(prepared, refs)

    assert rows[0]["clean"] is True
    assert rows[0]["pushed"] is False
    assert rows[0]["remote_ref"] == "refs/heads/main"


def test_local_checkpoint_repositories_fails_closed_on_git_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_root = tmp_path / "repos"
    create_remote_clone(github_root, "api", "main")
    adapter = LocalGitWorkspaceAdapter(github_root=github_root, state_root=tmp_path / "state")
    refs = (RepoRef("api", "main"),)
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)

    def failed_command(argv: Iterable[str], *, cwd: Path = None) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(list(argv), 1, "", "failed")
        return subprocess.run(
            list(argv),
            cwd=None if cwd is None else str(cwd),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    monkeypatch.setattr("awf.supervisor.workspace._command", failed_command)
    assert adapter.checkpoint_repositories(prepared, refs) == [
        {
            "repo": "api",
            "base": "main",
            "head": git_rev_parse(prepared.cwd / "api", "HEAD"),
            "remote_ref": "refs/heads/main",
            "clean": False,
            "pushed": False,
        }
    ]


def test_agentctl_checkpoint_repositories_attest_real_task_git_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    task_path = workspace_root / "tasks" / task_name
    task_path.mkdir(parents=True)
    create_remote_clone(repo_root, "api", "main")
    repository = create_remote_clone(
        workspace_root / "worktrees" / "api", task_name, "main"
    )
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )
    adapter = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    )
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)

    assert adapter.checkpoint_repositories(prepared, refs) == [
        {
            "repo": "api",
            "base": "main",
            "head": git_rev_parse(repository, "HEAD"),
            "remote_ref": "refs/heads/main",
            "clean": True,
            "pushed": True,
        }
    ]

@pytest.mark.parametrize("kind", ("dirty", "untracked", "unpushed", "remote-ref"))
def test_agentctl_checkpoint_repositories_fail_closed_for_unsafe_task_git_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    task_path = workspace_root / "tasks" / task_name
    task_path.mkdir(parents=True)
    create_remote_clone(repo_root, "api", "main")
    repository = create_remote_clone(
        workspace_root / "worktrees" / "api", task_name, "main"
    )
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )
    adapter = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    )
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)
    if kind == "dirty":
        (repository / "README.txt").write_text("changed\n", encoding="utf-8")
    elif kind == "untracked":
        (repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    elif kind == "unpushed":
        (repository / "commit.txt").write_text("commit\n", encoding="utf-8")
        _git(repository, "add", "commit.txt")
        _git(repository, "commit", "-m", "unpushed")
    else:
        advanced = _git(
            repository,
            "commit-tree",
            "HEAD^{tree}",
            "-p",
            "HEAD",
            "-m",
            "advance tracking",
        )
        _git(repository, "update-ref", "refs/remotes/origin/main", advanced)

    row = adapter.checkpoint_repositories(prepared, refs)[0]

    assert set(row) == {"repo", "base", "head", "remote_ref", "clean", "pushed"}
    assert row["remote_ref"] == "refs/heads/main"
    assert row["pushed"] is False
    assert row["clean"] is (kind not in ("dirty", "untracked"))


def test_agentctl_checkpoint_repositories_fail_closed_on_git_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _write_fake_agentctl(tmp_path)
    repo_root, workspace_root = _agentctl_roots(tmp_path)
    refs = (RepoRef("api", "main"),)
    task_name = _agentctl_task_name()
    task_path = workspace_root / "tasks" / task_name
    task_path.mkdir(parents=True)
    create_remote_clone(repo_root, "api", "main")
    create_remote_clone(workspace_root / "worktrees" / "api", task_name, "main")
    _agentctl_environment(
        monkeypatch,
        tmp_path,
        task_path=task_path,
        status=_agentctl_task_status(task_name, workspace_root, refs),
    )
    adapter = AgentctlWorkspaceAdapter(
        agentctl_path=executable, repo_root=repo_root, workspace_root=workspace_root
    )
    prepared = adapter.prepare(job_id="job-1", generation=1, repo_refs=refs)

    def failed_command(argv: Iterable[str], *, cwd: Path = None) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(list(argv), 1, "", "failed")
        return subprocess.run(
            list(argv),
            cwd=None if cwd is None else str(cwd),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    monkeypatch.setattr("awf.supervisor.workspace._command", failed_command)
    assert adapter.checkpoint_repositories(prepared, refs) == [
        {
            "repo": "api",
            "base": "main",
            "head": "",
            "remote_ref": "refs/heads/main",
            "clean": False,
            "pushed": False,
        }
    ]
