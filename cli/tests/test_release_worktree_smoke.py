from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient
from awf.worktrees.github import PullRequest
from awf.worktrees.models import DeploymentState, Purpose
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from worktree_fixtures import git as git_command
from worktree_fixtures import make_repository


@dataclass
class FakeGitHub:
    repository_root: Path
    prs: dict[int, PullRequest] = field(default_factory=dict)
    open_prs: dict[tuple[str, str], PullRequest] = field(default_factory=dict)

    def view_pr(self, number: int) -> PullRequest:
        return self.prs[number]

    def find_open_pr(self, *, head: str, base: str) -> PullRequest | None:
        return self.open_prs.get((head, base))

    def create_pr(
        self, *, base: str, head: str, title: str, body: str
    ) -> PullRequest:
        pull_request = PullRequest(
            number=900,
            state="OPEN",
            base_ref=base,
            base_sha="target-base",
            head_ref=head,
            head_sha=GitClient(self.repository_root).resolve_ref(head),
            merge_commit_sha=None,
            review_decision="",
            checks_passed=True,
            changed_paths=(),
            url="https://github.example/acme/repo/pull/900",
        )
        self.prs[pull_request.number] = pull_request
        self.open_prs[(head, base)] = pull_request
        return pull_request


@dataclass
class SmokeHarness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    state_dir: Path
    lock_dir: Path
    github: FakeGitHub
    config: WorktreeConfig
    deployment: DeploymentState
    deployment_calls: list[tuple[str, ...]]
    service: WorktreeService = field(init=False)

    @classmethod
    def create(cls, tmp_path: Path) -> SmokeHarness:
        repo = make_repository(tmp_path)
        git = GitClient(repo)
        git_command(repo, "branch", "main", "staging")
        git_command(repo, "push", "-q", "origin", "main")
        (repo / "team.txt").write_text("team\n", encoding="utf-8")
        git_command(repo, "add", "team.txt")
        git_command(repo, "commit", "-q", "-m", "staging team change")
        source_base_sha = git_command(repo, "rev-parse", "HEAD")
        git_command(repo, "checkout", "-q", "-b", "feature/pr-372")
        (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        git_command(repo, "add", "feature.txt")
        git_command(repo, "commit", "-q", "-m", "source feature")
        source_head_sha = git_command(repo, "rev-parse", "HEAD")
        git_command(repo, "push", "-q", "-u", "origin", "feature/pr-372")
        git_command(repo, "checkout", "-q", "staging")
        git_command(repo, "merge", "--no-ff", "-q", "feature/pr-372", "-m", "merge source")
        merged_sha = git_command(repo, "rev-parse", "HEAD")
        git_command(repo, "push", "-q", "origin", "staging")

        registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
        cache_dir = tmp_path / "cache"
        state_dir = tmp_path / "state"
        lock_dir = tmp_path / "locks"
        github = FakeGitHub(
            repository_root=repo,
            prs={
                372: PullRequest(
                    number=372,
                    state="MERGED",
                    base_ref="staging",
                    base_sha=source_base_sha,
                    head_ref="feature/pr-372",
                    head_sha=source_head_sha,
                    merge_commit_sha=merged_sha,
                    review_decision="APPROVED",
                    checks_passed=True,
                    changed_paths=("feature.txt",),
                    url="https://github.example/acme/repo/pull/372",
                )
            },
        )
        config = WorktreeConfig(
            default_base="staging",
            production_branch="main",
            verify_production=((sys.executable, "-c", "pass"),),
            deployment_status_command=("deployment-status",),
        )
        harness = cls(
            repo=repo,
            git=git,
            registry=registry,
            cache_dir=cache_dir,
            state_dir=state_dir,
            lock_dir=lock_dir,
            github=github,
            config=config,
            deployment=DeploymentState.UNKNOWN,
            deployment_calls=[],
        )
        harness._rebuild_service()
        return harness

    def _rebuild_service(self) -> None:
        self.service = WorktreeService(
            self.registry,
            self.git,
            config=self.config,
            github=self.github,
            deployment_runner=self._run_deployment_status,
            cache_dir=self.cache_dir,
            state_dir=self.state_dir,
            lock_dir=self.lock_dir,
        )

    def _run_deployment_status(
        self, argv: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.deployment_calls.append(tuple(argv))
        if self.deployment is DeploymentState.UNKNOWN:
            raise OSError("deployment status is unavailable")
        return subprocess.CompletedProcess(argv, 0, stdout="healthy\n", stderr="")

    def enable_verify_success(self) -> None:
        self.config = replace(
            self.config,
            verify_production=((sys.executable, "-c", "pass"),),
        )
        self._rebuild_service()

    def merge_target_pr(self, *, deployment: DeploymentState) -> None:
        target_pr = self.github.prs[900]
        self.github.prs[900] = replace(
            target_pr,
            state="MERGED",
            head_sha=self.git.resolve_ref(target_pr.head_ref),
            merge_commit_sha=self.git.resolve_ref(target_pr.head_ref),
        )
        self.deployment = deployment
        self.config = replace(
            self.config,
            deployment_status_command=(
                () if deployment is DeploymentState.UNKNOWN else ("deployment-status",)
            ),
        )
        self._rebuild_service()
        self.service.status(refresh=True)

    def set_deployment_healthy(self) -> None:
        self.deployment = DeploymentState.HEALTHY
        self.config = replace(
            self.config, deployment_status_command=("deployment-status",)
        )
        self._rebuild_service()


@pytest.fixture
def smoke(tmp_path: Path) -> SmokeHarness:
    return SmokeHarness.create(tmp_path)


def test_release_worktree_lifecycle_smoke(smoke: SmokeHarness) -> None:
    acquired = smoke.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="smoke",
        apply=True,
    )
    assert acquired.decision == "ready"

    promoted_preview = smoke.service.promote(
        source_pr=372, target_branch="main", apply=False
    )
    assert promoted_preview.decision == "preview"

    smoke.config = replace(smoke.config, verify_production=())
    smoke._rebuild_service()
    blocked_verify = smoke.service.promote(
        source_pr=372, target_branch="main", apply=True
    )
    assert blocked_verify.blockers[0]["code"] == "production_verify_missing"

    smoke.enable_verify_success()
    promoted = smoke.service.promote(source_pr=372, target_branch="main", apply=True)
    assert promoted.decision == "ready"
    assert promoted.lease is not None
    assert promoted.lease.target_pr == 900

    smoke.merge_target_pr(deployment=DeploymentState.UNKNOWN)
    blocked_cleanup = smoke.service.finish(
        pr_number=promoted.lease.target_pr, apply=True
    )
    assert blocked_cleanup.blockers[0]["code"] == "deployment_not_healthy"
    assert blocked_cleanup.lease is not None
    assert blocked_cleanup.lease.deployment_state is DeploymentState.UNKNOWN
    assert smoke.deployment_calls == []
    assert promoted.lease.worktree_path.exists()

    smoke.set_deployment_healthy()
    removed = smoke.service.finish(pr_number=promoted.lease.target_pr, apply=True)
    assert removed.decision == "removed"
    assert not promoted.lease.worktree_path.exists()
    assert smoke.deployment_calls == [
        ("deployment-status",),
        ("deployment-status",),
    ]
