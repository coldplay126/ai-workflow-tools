from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient, GitError, GitWorktree
from awf.worktrees.github import ExternalServiceError, PullRequest
from awf.worktrees.models import DeploymentState, Lease, LeaseState, Purpose
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from worktree_fixtures import git as git_command
from worktree_fixtures import make_repository


@dataclass
class FakeGitHub:
    prs: dict[int, PullRequest] = field(default_factory=dict)
    error: ExternalServiceError | None = None
    view_calls: list[int] = field(default_factory=list)
    create_calls: list[dict[str, str]] = field(default_factory=list)
    find_calls: list[tuple[str, str]] = field(default_factory=list)
    open_prs: dict[tuple[str, str], PullRequest] = field(default_factory=dict)
    repository_root: Path | None = None
    created_head_sha: str | None = None

    def view_pr(self, number: int) -> PullRequest:
        self.view_calls.append(number)
        if self.error is not None:
            raise self.error
        return self.prs[number]

    def find_open_pr(self, *, head: str, base: str) -> PullRequest | None:
        self.find_calls.append((head, base))
        return self.open_prs.get((head, base))

    def create_pr(
        self, *, base: str, head: str, title: str, body: str
    ) -> PullRequest:
        self.create_calls.append(
            {"base": base, "head": head, "title": title, "body": body}
        )
        pull_request = PullRequest(
            number=900,
            state="OPEN",
            base_ref=base,
            base_sha="target-base",
            head_ref=head,
            head_sha=(
                self.created_head_sha
                if self.created_head_sha is not None
                else (
                    GitClient(self.repository_root).resolve_ref(head)
                    if self.repository_root is not None
                    else "target-head"
                )
            ),
            merge_commit_sha=None,
            review_decision="",
            checks_passed=True,
            changed_paths=(),
            url="https://github.example/acme/repo/pull/900",
        )
        self.prs[pull_request.number] = pull_request
        self.open_prs[(head, base)] = pull_request
        return pull_request

def pull_request(
    *,
    number: int,
    state: str,
    head_sha: str,
    merge_commit_sha: str | None = None,
    base: str = "staging",
    changed_paths: tuple[str, ...] = (),
) -> PullRequest:
    return PullRequest(
        number=number,
        state=state,
        base_ref=base,
        base_sha="base-sha",
        head_ref="awf/reward-widget/feature",
        head_sha=head_sha,
        merge_commit_sha=merge_commit_sha,
        review_decision="APPROVED",
        checks_passed=True,
        changed_paths=changed_paths,
        url=f"https://github.example/acme/repo/pull/{number}",
    )


def merged_pr(**kwargs: object) -> PullRequest:
    return pull_request(state="MERGED", merge_commit_sha="merge-sha", **kwargs)


def closed_pr(**kwargs: object) -> PullRequest:
    return pull_request(state="CLOSED", **kwargs)


@dataclass
class Harness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    state_dir: Path
    lock_dir: Path
    github: FakeGitHub
    service: WorktreeService

    @classmethod
    def create(cls, tmp_path: Path) -> Harness:
        repo = make_repository(tmp_path)
        git = GitClient(repo)
        registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
        cache_dir = tmp_path / "cache"
        state_dir = tmp_path / "state"
        lock_dir = tmp_path / "locks"
        github = FakeGitHub()
        return cls(
            repo=repo,
            git=git,
            registry=registry,
            cache_dir=cache_dir,
            state_dir=state_dir,
            lock_dir=lock_dir,
            github=github,
            service=WorktreeService(
                registry,
                git,
                config=WorktreeConfig(default_base="staging"),
                github=github,
                cache_dir=cache_dir,
                state_dir=state_dir,
                lock_dir=lock_dir,
            ),
        )

    def acquire(self, initiative: str):
        return self.service.acquire(
            initiative=initiative,
            purpose=Purpose.FEATURE,
            base=None,
            branch=None,
            owner_id="session-1",
            apply=True,
        )

    def attach_pr(self, lease: Lease, number: int) -> Lease:
        return self.registry.transition(
            lease.id,
            LeaseState.PR_OPEN,
            expected_version=lease.version,
            pr_number=number,
        )

    def enable_prepare_command(self, log: Path, message: str = "prepared") -> None:
        script = (
            "from pathlib import Path; "
            f"Path({str(log)!r}).open('a', encoding='utf-8').write({message + chr(10)!r})"
        )
        self.service = WorktreeService(
            self.registry,
            self.git,
            config=WorktreeConfig(
                default_base="staging",
                prepare_inputs=("README.txt",),
                prepare_command=(sys.executable, "-c", script),
            ),
            cache_dir=self.cache_dir,
            state_dir=self.state_dir,
            lock_dir=self.lock_dir,
        )
    def make_external_worktree(self, branch: str) -> Path:
        external = self.repo.parent / branch
        git_command(
            self.repo,
            "worktree",
            "add",
            "-q",
            "-b",
            branch,
            str(external),
            "staging",
        )
        return external

    def import_external(self, branch: str) -> Lease:
        external = self.make_external_worktree(branch)
        result = self.service.import_root(self.repo.parent, apply=True)
        return next(lease for lease in result.leases if lease.worktree_path == external)

@dataclass
class PromotionHarness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    state_dir: Path
    lock_dir: Path
    github: FakeGitHub
    config: WorktreeConfig
    service: WorktreeService
    source_base_sha: str
    source_head_sha: str

    @classmethod
    def create(cls, tmp_path: Path, *, aggregate: bool = False) -> PromotionHarness:
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
        changed_paths = ("feature.txt",)
        if aggregate:
            git_command(repo, "checkout", "-q", "staging")
            git_command(repo, "checkout", "-q", "-b", "support/pr-372")
            (repo / "support.txt").write_text("support\n", encoding="utf-8")
            git_command(repo, "add", "support.txt")
            git_command(repo, "commit", "-q", "-m", "source support")
            git_command(repo, "checkout", "-q", "feature/pr-372")
            git_command(
                repo, "merge", "--no-ff", "-q", "support/pr-372", "-m", "aggregate source"
            )
            changed_paths = ("feature.txt", "support.txt")
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
                    changed_paths=changed_paths,
                    url="https://github.example/acme/repo/pull/372",
                )
            }
        )
        config = WorktreeConfig(
            default_base="staging",
            production_branch="main",
            verify_production=((sys.executable, "-c", "pass"),),
        )
        return cls(
            repo=repo,
            git=git,
            registry=registry,
            cache_dir=cache_dir,
            state_dir=state_dir,
            lock_dir=lock_dir,
            github=github,
            config=config,
            service=WorktreeService(
                registry,
                git,
                config=config,
                github=github,
                cache_dir=cache_dir,
                state_dir=state_dir,
                lock_dir=lock_dir,
            ),
            source_base_sha=source_base_sha,
            source_head_sha=source_head_sha,
        )

    def configure(self, **values: object) -> None:
        self.config = replace(self.config, **values)
        self.service = WorktreeService(
            self.registry,
            self.git,
            config=self.config,
            github=self.github,
            cache_dir=self.cache_dir,
            state_dir=self.state_dir,
            lock_dir=self.lock_dir,
        )

    def make_target_conflict(self) -> None:
        git_command(self.repo, "checkout", "-q", "main")
        (self.repo / "feature.txt").write_text("target\n", encoding="utf-8")
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "target feature")
        git_command(self.repo, "push", "-q", "origin", "main")
        git_command(self.repo, "checkout", "-q", "staging")
def test_github_client_marks_checks_passed_only_for_completed_successes(
    harness: Harness,
) -> None:
    from awf.worktrees.github import GhClient
    calls: list[tuple[list[str], dict[str, object]]] = []


    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "number": 42,
                    "state": "OPEN",
                    "baseRefName": "staging",
                    "baseRefOid": "base-sha",
                    "headRefName": "awf/reward-widget/feature",
                    "headRefOid": "head-sha",
                    "mergeCommit": None,
                    "reviewDecision": "APPROVED",
                    "statusCheckRollup": [
                        {"status": "COMPLETED", "conclusion": "SUCCESS"},
                        {"status": "COMPLETED", "conclusion": "SKIPPED"},
                        {"status": "COMPLETED", "conclusion": "NEUTRAL"},
                    ],
                    "files": [{"path": "README.txt"}],
                    "url": "https://github.example/acme/repo/pull/42",
                }
            ),
            "",
        )

    pr = GhClient(harness.repo, command_runner=runner).view_pr(42)

    assert calls == [
        (
            [
                "gh",
                "pr",
                "view",
                "42",
                "--json",
                (
                    "number,state,baseRefName,baseRefOid,headRefName,headRefOid,"
                    "mergeCommit,reviewDecision,statusCheckRollup,files,url"
                ),
            ],
            {
                "cwd": str(harness.repo.resolve()),
                "check": False,
                "shell": False,
                "capture_output": True,
                "text": True,
                "timeout": 30.0,
            },
        )
    ]
    assert pr.checks_passed is True
    assert pr.changed_paths == ("README.txt",)


def test_github_client_finds_exact_open_pr_by_head_and_base(
    harness: Harness,
) -> None:
    from awf.worktrees.github import GhClient

    calls: list[list[str]] = []

    def runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                [
                    {
                        "number": 42,
                        "state": "OPEN",
                        "baseRefName": "main",
                        "baseRefOid": "base-sha",
                        "headRefName": "awf/pr-372-to-main/promote",
                        "headRefOid": "head-sha",
                        "mergeCommit": None,
                        "reviewDecision": "",
                        "statusCheckRollup": [],
                        "files": [],
                        "url": "https://github.example/acme/repo/pull/42",
                    }
                ]
            ),
            "",
        )

    pull_request = GhClient(harness.repo, command_runner=runner).find_open_pr(
        head="awf/pr-372-to-main/promote", base="main"
    )

    assert pull_request is not None
    assert pull_request.number == 42
    assert calls[0][:9] == [
        "gh",
        "pr",
        "list",
        "--state",
        "open",
        "--base",
        "main",
        "--head",
        "awf/pr-372-to-main/promote",
    ]


@pytest.mark.parametrize(
    "check",
    (
        {"status": "IN_PROGRESS", "conclusion": None},
        {"status": "COMPLETED", "conclusion": None},
        {"conclusion": "SUCCESS"},
    ),
)
def test_github_client_rejects_pending_or_incomplete_checks(
    harness: Harness, check: dict[str, str | None]
) -> None:
    from awf.worktrees.github import GhClient

    def runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "number": 42,
                    "state": "OPEN",
                    "baseRefName": "staging",
                    "baseRefOid": "base-sha",
                    "headRefName": "awf/reward-widget/feature",
                    "headRefOid": "head-sha",
                    "mergeCommit": None,
                    "reviewDecision": "APPROVED",
                    "statusCheckRollup": [check],
                    "files": [],
                    "url": "https://github.example/acme/repo/pull/42",
                }
            ),
            "",
        )

    assert GhClient(harness.repo, command_runner=runner).view_pr(42).checks_passed is False


def test_refresh_marks_merged_feature_cleanable(harness: Harness) -> None:
    acquired = harness.acquire("reward-widget")
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(
        number=42,
        base="staging",
        head_sha=lease.head_sha,
        changed_paths=("README.txt",),
    )

    first = harness.service.status(initiative="reward-widget", refresh=True)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.CLEANABLE
    assert first.leases[0].deployment_state is DeploymentState.NOT_REQUIRED
    assert second.leases[0].version == first.leases[0].version


def test_refresh_does_not_churn_an_unchanged_open_pr(harness: Harness) -> None:
    acquired = harness.acquire("reward-widget")
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = pull_request(
        number=42,
        state="OPEN",
        head_sha=lease.head_sha,
    )

    first = harness.service.status(initiative="reward-widget", refresh=True)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.PR_OPEN
    assert second.leases[0].version == first.leases[0].version


def test_refresh_marks_closed_unmerged_without_deleting(harness: Harness) -> None:
    acquired = harness.acquire("reward-widget")
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = closed_pr(number=42, head_sha=lease.head_sha)

    first = harness.service.status(initiative="reward-widget", refresh=True)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.CLOSED_UNMERGED
    assert lease.worktree_path.exists()
    assert second.leases[0].version == first.leases[0].version


def test_refresh_external_failure_keeps_previous_state_and_warns(
    harness: Harness,
) -> None:
    acquired = harness.acquire("reward-widget")
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.error = ExternalServiceError("gh auth required")

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is lease.state
    assert result.warnings[0]["code"] == "github_refresh_failed"


def test_refresh_promoted_lease_cleanable_after_healthy_deployment(
    harness: Harness,
) -> None:
    acquired = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(number=42, head_sha=lease.head_sha)

    deployment_calls: list[list[str]] = []

    def deployment_runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        deployment_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    harness.service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(
            default_base="staging", deployment_status_command=("deploy", "status")
        ),
        github=harness.github,
        deployment_runner=deployment_runner,
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
    )

    first = harness.service.status(initiative="reward-widget", refresh=True)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.CLEANABLE
    assert first.leases[0].deployment_state is DeploymentState.HEALTHY
    assert second.leases[0].version == first.leases[0].version
    assert deployment_calls == [["deploy", "status"]]


def test_refresh_blocks_promoted_lease_when_deployment_fails(
    harness: Harness,
) -> None:
    acquired = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(number=42, head_sha=lease.head_sha)

    def deployment_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "deployment failed")

    harness.service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(
            default_base="staging", deployment_status_command=("deploy", "status")
        ),
        github=harness.github,
        deployment_runner=deployment_runner,
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
    )

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is LeaseState.BLOCKED
    assert result.leases[0].deployment_state is DeploymentState.FAILED


def test_refresh_keeps_promoted_lease_deploying_without_status_command(
    harness: Harness,
) -> None:
    acquired = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(number=42, head_sha=lease.head_sha)

    first = harness.service.status(initiative="reward-widget", refresh=True)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.DEPLOYING
    assert first.leases[0].deployment_state is DeploymentState.UNKNOWN
    assert second.leases[0].version == first.leases[0].version

@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness.create(tmp_path)


def test_refresh_warns_when_github_returns_a_different_pr_number(
    harness: Harness,
) -> None:
    acquired = harness.acquire("reward-widget")
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(number=43, head_sha=lease.head_sha)

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is LeaseState.PR_OPEN
    assert result.warnings[0]["code"] == "github_refresh_failed"


def test_refresh_warns_when_compare_and_swap_loses_a_race(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired = harness.acquire("reward-widget")
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(number=42, head_sha=lease.head_sha)

    def conflict(*_: object, **__: object) -> Lease:
        raise RuntimeError("lease changed concurrently")

    monkeypatch.setattr(harness.registry, "transition", conflict)

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is LeaseState.PR_OPEN
    assert result.warnings[0]["code"] == "lease_refresh_failed"


def test_refresh_warns_after_a_bounded_deployment_timeout(
    harness: Harness,
) -> None:
    acquired = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(number=42, head_sha=lease.head_sha)
    timeouts: list[float] = []

    def deployment_runner(
        command: list[str], *, timeout: float, **_: object
    ) -> subprocess.CompletedProcess[str]:
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired(command, timeout)

    harness.service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(
            default_base="staging", deployment_status_command=("deploy", "status")
        ),
        github=harness.github,
        deployment_runner=deployment_runner,
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
    )

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is LeaseState.DEPLOYING
    assert result.leases[0].deployment_state is DeploymentState.PENDING
    assert result.warnings[0]["code"] == "deployment_refresh_failed"
    assert timeouts == [30.0]


def test_acquire_previews_without_creating_a_worktree(harness: Harness) -> None:
    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="session-1",
        apply=False,
    )

    assert result.decision == "preview"
    assert result.actions[0]["kind"] == "create_worktree"
    assert len(harness.git.list_worktrees()) == 1
    assert harness.registry.list_leases() == []
    assert not harness.cache_dir.exists()


def test_acquire_creates_one_managed_worktree(harness: Harness) -> None:
    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.branch == "awf/reward-widget/feature"
    assert result.lease.worktree_path.exists()
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_reuses_the_exact_active_lease(harness: Harness) -> None:
    first = harness.acquire("reward-widget")
    second = harness.acquire("reward-widget")

    assert second.decision == "reuse"
    assert second.lease is not None
    assert first.lease is not None
    assert second.lease.id == first.lease.id
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_blocks_when_registered_path_is_missing(harness: Harness) -> None:
    first = harness.acquire("reward-widget")
    assert first.lease is not None
    subprocess.run(
        ["git", "worktree", "remove", str(first.lease.worktree_path)],
        cwd=harness.repo,
        check=True,
    )

    result = harness.acquire("reward-widget")

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "orphaned_lease"


def test_acquire_prepares_new_worktree_and_reuses_matching_prepare_key(
    harness: Harness, tmp_path: Path
) -> None:
    log = tmp_path / "prepare.log"
    harness.enable_prepare_command(log)

    first = harness.acquire("reward-widget")
    second = harness.acquire("reward-widget")

    assert first.decision == "ready"
    assert second.decision == "reuse"
    assert log.read_text(encoding="utf-8").splitlines() == ["prepared"]


def test_acquire_blocks_for_a_conflicting_requested_branch(harness: Harness) -> None:
    harness.acquire("reward-widget")

    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch="awf/reward-widget/alternate",
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "lease_conflict"



def test_acquire_removes_a_clean_worktree_when_registry_insert_fails(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("registry is unavailable")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_conflict"
    assert len(git.list_worktrees()) == 1


def test_acquire_removes_a_clean_worktree_when_registry_filesystem_insert_fails(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise PermissionError("state directory is read-only")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_conflict"
    assert len(git.list_worktrees()) == 1


def test_acquire_preserves_a_dirty_worktree_when_registry_filesystem_insert_fails(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            (lease.worktree_path / "recovery.txt").write_text(
                "keep this worktree\n",
                encoding="utf-8",
            )
            raise PermissionError("state directory is read-only")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_conflict"
    assert len(git.list_worktrees()) == 2


def test_acquire_retry_succeeds_after_clean_registry_recovery(
    tmp_path: Path,
) -> None:
    class FlakyRegistry(WorktreeRegistry):
        failed = False

        def create_lease(self, lease: Lease) -> Lease:
            if not self.failed:
                self.failed = True
                raise sqlite3.IntegrityError("registry is unavailable")
            return super().create_lease(lease)

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FlakyRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )
    request = {
        "initiative": "reward-widget",
        "purpose": Purpose.FEATURE,
        "base": None,
        "branch": None,
        "owner_id": "session-1",
        "apply": True,
    }

    failed = service.acquire(**request)
    recovered = service.acquire(**request)

    assert failed.status == "blocked"
    assert recovered.decision == "ready"
    assert len(git.list_worktrees()) == 2


@pytest.mark.parametrize(
    "base",
    [
        "main:refs/heads/unrelated",
        "+main",
        "main*",
        "main^{commit}",
    ],
)
def test_acquire_rejects_unsafe_base_before_fetch(
    tmp_path: Path, base: str
) -> None:
    class NoFetchGit(GitClient):
        def fetch_ref(self, ref: str) -> str:
            raise AssertionError(f"fetch_ref unexpectedly called with {ref!r}")

    repo = make_repository(tmp_path)
    git = NoFetchGit(repo)
    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    with pytest.raises(ValueError, match="invalid base ref"):
        service.acquire(
            initiative="reward-widget",
            purpose=Purpose.FEATURE,
            base=base,
            branch=None,
            owner_id="session-1",
            apply=False,
        )


def test_acquire_reports_branch_cleanup_failure_after_registry_insert_failure(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("registry is unavailable")

    class BranchDeleteFailingGit(GitClient):
        def delete_branch_if_at(self, branch: str, expected_sha: str) -> None:
            raise GitError(f"could not delete {branch}")

    repo = make_repository(tmp_path)
    git = BranchDeleteFailingGit(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_recovery_failed"
    assert "branch cleanup failed" in result.blockers[0]["message"]
    assert len(git.list_worktrees()) == 1


def test_acquire_reprepares_when_the_prepare_key_changes(
    harness: Harness, tmp_path: Path
) -> None:
    log = tmp_path / "prepare.log"
    harness.enable_prepare_command(log, "first")

    first = harness.acquire("reward-widget")
    harness.enable_prepare_command(log, "second")
    second = harness.acquire("reward-widget")

    assert first.decision == "ready"
    assert second.decision == "reuse"
    assert log.read_text(encoding="utf-8").splitlines() == ["first", "second"]


def test_acquire_blocks_prepare_failure_without_writing_a_marker(
    harness: Harness,
) -> None:
    harness.service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(
            default_base="staging",
            prepare_command=(sys.executable, "-c", "import sys; sys.exit(7)"),
        ),
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
    )

    result = harness.acquire("reward-widget")

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "prepare_failed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert not (harness.state_dir / "prepare" / f"{result.lease.id}.json").exists()
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_retry_succeeds_after_divergent_base_registry_recovery(
    tmp_path: Path,
) -> None:
    class FlakyRegistry(WorktreeRegistry):
        failed = False

        def create_lease(self, lease: Lease) -> Lease:
            if not self.failed:
                self.failed = True
                raise sqlite3.IntegrityError("registry is unavailable")
            return super().create_lease(lease)

    repo = make_repository(tmp_path)
    git_command(repo, "checkout", "-q", "-b", "release")
    (repo / "release.txt").write_text("release\n", encoding="utf-8")
    git_command(repo, "add", "release.txt")
    git_command(repo, "commit", "-q", "-m", "release")
    git_command(repo, "push", "-q", "-u", "origin", "release")
    git_command(repo, "checkout", "-q", "staging")
    git = GitClient(repo)
    service = WorktreeService(
        FlakyRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )
    request = {
        "initiative": "reward-widget",
        "purpose": Purpose.FEATURE,
        "base": "release",
        "branch": None,
        "owner_id": "session-1",
        "apply": True,
    }

    failed = service.acquire(**request)
    recovered = service.acquire(**request)

    assert failed.status == "blocked"
    assert recovered.decision == "ready"
    assert len(git.list_worktrees()) == 2


def test_acquire_preserves_a_clean_worktree_with_a_new_commit_after_registry_failure(
    tmp_path: Path,
) -> None:
    class CommittingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            (lease.worktree_path / "recovery.txt").write_text(
                "retain this commit\n",
                encoding="utf-8",
            )
            git_command(lease.worktree_path, "add", "recovery.txt")
            git_command(lease.worktree_path, "commit", "-q", "-m", "recovery")
            raise sqlite3.IntegrityError("registry is unavailable")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        CommittingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_recovery_failed"
    assert "head changed" in result.blockers[0]["message"]
    assert result.lease is not None
    assert result.lease.worktree_path.is_dir()
    assert git_command(repo, "rev-parse", result.lease.branch) == git_command(
        result.lease.worktree_path, "rev-parse", "HEAD"
    )


def test_acquire_preserves_branch_when_rollback_cas_detects_a_race(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("registry is unavailable")

    class RacingGit(GitClient):
        def delete_branch_if_at(self, branch: str, expected_sha: str) -> None:
            git_command(
                self.cwd,
                "update-ref",
                f"refs/heads/{branch}",
                self.head_sha(),
            )
            super().delete_branch_if_at(branch, expected_sha)

    repo = make_repository(tmp_path)
    git_command(repo, "checkout", "-q", "-b", "release")
    (repo / "release.txt").write_text("release\n", encoding="utf-8")
    git_command(repo, "add", "release.txt")
    git_command(repo, "commit", "-q", "-m", "release")
    git_command(repo, "push", "-q", "-u", "origin", "release")
    git_command(repo, "checkout", "-q", "staging")
    git = RacingGit(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="release",
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_recovery_failed"
    assert "branch cleanup failed" in result.blockers[0]["message"]
    assert len(git.list_worktrees()) == 1
    assert git_command(repo, "rev-parse", "awf/reward-widget/feature") == git_command(
        repo, "rev-parse", "staging"
    )


def test_import_registers_existing_worktree_as_unmanaged(harness: Harness) -> None:
    external = harness.make_external_worktree("legacy-release")

    result = harness.service.import_root(harness.repo.parent, apply=True)

    imported = next(item for item in result.leases if item.worktree_path == external)
    assert imported.managed is False
    assert imported.owner_kind == "imported"
    assert imported.purpose is Purpose.SCRATCH


def test_import_dry_run_writes_nothing(harness: Harness) -> None:
    harness.make_external_worktree("legacy-release")

    result = harness.service.import_root(harness.repo.parent, apply=False)

    assert result.decision == "preview"
    assert harness.registry.list_leases() == []


def test_adopt_refuses_dirty_imported_worktree(harness: Harness) -> None:
    imported = harness.import_external("legacy-release")
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = harness.service.adopt(imported.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "dirty_worktree"
    assert harness.registry.get_lease(imported.id).managed is False


def test_adopt_marks_clean_imported_worktree_managed(harness: Harness) -> None:
    imported = harness.import_external("legacy-release")

    result = harness.service.adopt(imported.id, apply=True)

    assert result.decision == "ready"
    assert result.lease.managed is True
    assert result.lease.owner_kind == "imported"


def test_doctor_reports_registered_head_dirty_cache_and_skill_link_mismatches(
    harness: Harness, tmp_path: Path
) -> None:
    imported = harness.import_external("legacy-release")
    stale = harness.registry.transition(
        imported.id,
        imported.state,
        expected_version=imported.version,
        head_sha="0" * 40,
    )
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    abandoned_cache_dir = harness.cache_dir / harness.repo.name / "abandoned"
    abandoned_cache_dir.mkdir(parents=True)
    harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.git.repository_root(),
            worktree_path=abandoned_cache_dir,
            initiative="cache-abandoned",
            purpose=Purpose.FEATURE,
            branch="awf/cache-abandoned/feature",
            base_ref="origin/staging",
            head_sha=harness.git.head_sha(),
            managed=True,
            owner_kind="awf",
        )
    )
    skill_source = tmp_path / "release-worktree-lifecycle"
    skill_source.mkdir()
    home_dir = tmp_path / "home"
    claude_link = home_dir / ".claude" / "skills" / "release-worktree-lifecycle"
    claude_link.parent.mkdir(parents=True)
    claude_link.symlink_to(skill_source, target_is_directory=True)
    wrong_target = tmp_path / "wrong-skill"
    wrong_target.mkdir()
    agents_link = home_dir / ".agents" / "skills" / "release-worktree-lifecycle"
    agents_link.parent.mkdir(parents=True)
    agents_link.symlink_to(wrong_target, target_is_directory=True)
    service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
        skill_source_dir=skill_source,
        home_dir=home_dir,
    )

    result = service.doctor()

    actions_by_kind = {
        action["kind"]: action for action in result.actions
    }
    assert actions_by_kind["head_mismatch"]["lease_id"] == stale.id
    assert actions_by_kind["dirty_worktree"]["path"] == str(
        imported.worktree_path
    )
    assert actions_by_kind["unregistered_cache_directory"]["path"] == str(
        abandoned_cache_dir
    )
    assert actions_by_kind["wrong_skill_link"]["path"] == str(agents_link)
    assert harness.registry.get_lease(imported.id).version == stale.version


def test_adopt_preview_does_not_create_lock_or_registry_state(
    harness: Harness
) -> None:
    external = harness.make_external_worktree("legacy-release")
    lease = harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.git.repository_root(),
            worktree_path=external,
            initiative="import-legacy-release-12345678",
            purpose=Purpose.SCRATCH,
            branch="legacy-release",
            base_ref="legacy-release",
            head_sha=harness.git.head_sha(external),
            managed=False,
            owner_kind="imported",
        )
    )
    assert not harness.lock_dir.exists()

    result = harness.service.adopt(lease.id, apply=False)

    assert result.decision == "preview"
    assert result.actions[0]["kind"] == "adopt"
    assert not harness.lock_dir.exists()
    assert harness.registry.get_lease(lease.id).version == lease.version


def test_import_keeps_exact_initiative_and_skips_identity_collisions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    candidate = root / "repo"
    (candidate / ".git").mkdir(parents=True)
    first_path = candidate / "first"
    second_path = candidate / "second"
    first_path.mkdir()
    second_path.mkdir()
    head_sha = "0123456789abcdef0123456789abcdef01234567"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return candidate

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(first_path, head_sha, "topic/release"),
                GitWorktree(second_path, head_sha, "topic/release"),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    service = WorktreeService(
        registry,
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    preview = service.import_root(root, apply=False)
    result = service.import_root(root, apply=True)

    base = "import-topic-release-01234567"
    assert [lease.initiative for lease in preview.leases] == [base]
    assert [lease.worktree_path for lease in preview.leases] == [first_path]
    assert [lease.worktree_path for lease in result.leases] == [first_path]
    assert any(
        action == {
            "kind": "skipped_identity_collision",
            "path": str(second_path),
            "repository_id": "repository-id",
            "initiative": base,
            "purpose": "scratch",
        }
        for action in preview.actions
    )
    assert [lease.worktree_path for lease in registry.list_leases()] == [first_path]


def test_doctor_reports_dirty_detached_registered_worktree(
    harness: Harness,
) -> None:
    imported = harness.import_external("legacy-release")
    git_command(imported.worktree_path, "checkout", "-q", "--detach")
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = harness.service.doctor()

    assert any(
        action["kind"] == "dirty_worktree"
        and action["path"] == str(imported.worktree_path)
        for action in result.actions
    )


def test_adopt_blocks_branch_checked_out_at_another_registered_path(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "registered"
    duplicate_path = tmp_path / "duplicate"
    worktree_path.mkdir()
    duplicate_path.mkdir()
    head_sha = "1234567890abcdef1234567890abcdef12345678"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(worktree_path, head_sha, "topic/release"),
                GitWorktree(duplicate_path, head_sha, "topic/release"),
            )

        def head_sha(self, cwd: Path) -> str:
            return head_sha

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    lease = registry.create_lease(
        Lease.new(
            repository_id="repository-id",
            repository_name="repo",
            repository_root=tmp_path,
            worktree_path=worktree_path,
            initiative="import-topic-release-12345678",
            purpose=Purpose.SCRATCH,
            branch="topic/release",
            base_ref="topic/release",
            head_sha=head_sha,
            managed=False,
            owner_kind="imported",
        )
    )
    service = WorktreeService(
        registry,
        FakeGit(),
        config=WorktreeConfig(),
        lock_dir=tmp_path / "locks",
    )

    result = service.adopt(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "branch_conflict"
    assert registry.get_lease(lease.id).managed is False


def test_import_uses_non_bare_worktree_metadata_when_primary_is_bare(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    candidate = root / "repo"
    (candidate / ".git").mkdir(parents=True)
    bare_path = tmp_path / "origin.git"
    bare_path.mkdir()
    worktree_path = candidate / "linked"
    worktree_path.mkdir()
    head_sha = "abcdef0123456789abcdef0123456789abcdef01"

    class CandidateGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return candidate

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(bare_path, None, None, bare=True),
                GitWorktree(worktree_path, head_sha, "topic/release"),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    class BareGit:
        def repository_id(self) -> str:
            raise AssertionError("bare worktree metadata must not be used")

    candidate_git = CandidateGit()
    bare_git = BareGit()
    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda path: (
            bare_git if path.resolve() == bare_path.resolve() else candidate_git
        ),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert [lease.worktree_path for lease in result.leases] == [worktree_path]
    assert result.leases[0].repository_root == candidate


def test_import_skips_prunable_and_missing_worktree_registrations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    candidate = root / "repo"
    (candidate / ".git").mkdir(parents=True)
    valid_path = candidate / "valid"
    valid_path.mkdir()
    prunable_path = candidate / "prunable"
    missing_path = candidate / "missing"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return candidate

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(valid_path, "1111111111111111111111111111111111111111", "valid"),
                GitWorktree(
                    prunable_path,
                    "2222222222222222222222222222222222222222",
                    "prunable",
                    prunable="gitdir file is missing",
                ),
                GitWorktree(
                    missing_path,
                    "3333333333333333333333333333333333333333",
                    "missing",
                ),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert [lease.worktree_path for lease in result.leases] == [valid_path]


def test_doctor_reports_duplicate_branch_registrations(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    head_sha = "1234567890abcdef1234567890abcdef12345678"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(first_path, head_sha, "topic/release"),
                GitWorktree(second_path, head_sha, "topic/release"),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    for path, initiative in (
        (first_path, "import-first-12345678"),
        (second_path, "import-second-12345678"),
    ):
        registry.create_lease(
            Lease.new(
                repository_id="repository-id",
                repository_name="repo",
                repository_root=tmp_path,
                worktree_path=path,
                initiative=initiative,
                purpose=Purpose.SCRATCH,
                branch="topic/release",
                base_ref="topic/release",
                head_sha=head_sha,
                managed=False,
                owner_kind="imported",
            )
        )
    service = WorktreeService(
        registry,
        FakeGit(),
        config=WorktreeConfig(),
        lock_dir=tmp_path / "locks",
    )

    result = service.doctor()

    assert {
        "kind": "duplicate_branch",
        "branch": "topic/release",
        "paths": [str(first_path), str(second_path)],
    } in result.actions


def test_import_apply_revalidates_changed_worktree_under_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    snapshot_head = "1111111111111111111111111111111111111111"
    current_head = "2222222222222222222222222222222222222222"

    class ChangingGit:
        list_calls = 0

        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            self.list_calls += 1
            head_sha = snapshot_head if self.list_calls == 1 else current_head
            return (GitWorktree(worktree_path, head_sha, "topic/release"),)

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return () if self.list_calls == 1 else (" M changed.txt",)

    git = ChangingGit()
    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: git,
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert git.list_calls >= 2
    assert result.leases[0].head_sha == current_head
    assert result.leases[0].state is LeaseState.DIRTY
    assert [
        action for action in result.actions if action["kind"] == "import_worktree"
    ] == [
        {
            "kind": "import_worktree",
            "path": str(worktree_path),
            "lease_id": result.leases[0].id,
        }
    ]


def test_import_apply_skips_worktree_that_disappears_before_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    head_sha = "1111111111111111111111111111111111111111"

    class VanishingGit:
        list_calls = 0

        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            self.list_calls += 1
            return (
                (GitWorktree(worktree_path, head_sha, "topic/release"),)
                if self.list_calls == 1
                else ()
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()
    git = VanishingGit()

    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: git,
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert result.leases == ()
    assert any(
        action["kind"] == "skipped_unavailable_worktree"
        and action["path"] == str(worktree_path)
        for action in result.actions
    )
    assert not any(
        action["kind"] == "import_worktree" for action in result.actions
    )


def test_import_apply_converts_active_identity_race_to_skipped_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    head_sha = "1111111111111111111111111111111111111111"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (GitWorktree(worktree_path, head_sha, "topic/release"),)

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    competing_path = tmp_path / "competing"
    competing_path.mkdir()
    competing = Lease.new(
        repository_id="repository-id",
        repository_name="repo",
        repository_root=worktree_path,
        worktree_path=competing_path,
        initiative="import-topic-release-11111111",
        purpose=Purpose.SCRATCH,
        branch="topic/release",
        base_ref="topic/release",
        head_sha=head_sha,
        managed=False,
        owner_kind="imported",
    )

    class RacingRegistry(WorktreeRegistry):
        def find_active(
            self, repository_id: str, initiative: str, purpose: Purpose
        ) -> Lease | None:
            return competing

    registry = RacingRegistry(tmp_path / "state" / "worktrees.sqlite3")
    service = WorktreeService(
        registry,
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert result.leases == ()
    assert any(
        action["kind"] == "skipped_identity_collision"
        and action["path"] == str(worktree_path)
        for action in result.actions
    )
    assert not any(
        action["kind"] == "import_worktree" for action in result.actions
    )


def test_doctor_ignores_same_name_cache_child_from_other_repository(
    harness: Harness,
) -> None:
    foreign_cache_child = harness.cache_dir / harness.repo.name / "foreign"
    foreign_cache_child.mkdir(parents=True)
    harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.git.repository_root(),
            worktree_path=foreign_cache_child,
            initiative="foreign-cache",
            purpose=Purpose.FEATURE,
            branch="awf/foreign-cache/feature",
            base_ref="origin/staging",
            head_sha=harness.git.head_sha(),
            managed=True,
            owner_kind="awf",
        )
    )

    class ForeignGit:
        def repository_id(self) -> str:
            return "foreign-repository-id"

    service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
        git_factory=lambda path: (
            ForeignGit()
            if path.resolve() == foreign_cache_child.resolve()
            else GitClient(path)
        ),
    )

    result = service.doctor()

    assert not any(
        action["kind"] == "unregistered_cache_directory"
        and action["path"] == str(foreign_cache_child)
        for action in result.actions
    )


def test_import_apply_converts_insertion_identity_race_to_skipped_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    head_sha = "1111111111111111111111111111111111111111"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (GitWorktree(worktree_path, head_sha, "topic/release"),)

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    class InsertionRacingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("active identity inserted concurrently")

    service = WorktreeService(
        InsertionRacingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert result.leases == ()
    assert any(
        action["kind"] == "skipped_identity_collision"
        and action["path"] == str(worktree_path)
        for action in result.actions
    )
    assert not any(
        action["kind"] == "import_worktree" for action in result.actions
    )


@pytest.fixture
def promotion_harness(tmp_path: Path) -> PromotionHarness:
    return PromotionHarness.create(tmp_path)


def test_promote_applies_only_source_pr_delta(
    promotion_harness: PromotionHarness,
) -> None:
    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    worktree = result.lease.worktree_path
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert not (worktree / "team.txt").exists()
    assert promotion_harness.git.changed_paths(worktree, result.lease.base_ref) == (
        "feature.txt",
    )
    assert promotion_harness.github.create_calls[0]["base"] == "main"
    assert "AWF-Source-PR: 372" in promotion_harness.github.create_calls[0]["body"]
    assert result.lease.state is LeaseState.PR_OPEN
    assert result.lease.target_pr == 900


def test_promote_requires_merged_source_pr(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372], state="OPEN"
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_not_merged"
    assert not promotion_harness.registry.db_path.exists()


def test_promote_requires_production_verify_commands(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(verify_production=())

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "production_verify_missing"
    assert promotion_harness.github.create_calls == []


def test_promote_preserves_conflicted_worktree(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert result.lease.worktree_path.exists()
    assert promotion_harness.github.create_calls == []


def test_promote_preview_avoids_mutation(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_fetch(_: str) -> str:
        raise AssertionError("promotion preview must not fetch")

    monkeypatch.setattr(promotion_harness.git, "fetch_ref", unexpected_fetch)

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert result.decision == "preview"
    assert result.actions[0]["source_head_sha"] == promotion_harness.source_head_sha
    assert result.actions[0]["target_branch"] == "main"
    assert len(promotion_harness.git.list_worktrees()) == 1
    assert not promotion_harness.registry.db_path.exists()
    assert not promotion_harness.cache_dir.exists()
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize(
    ("source_change", "blocker"),
    (
        ({"review_decision": "CHANGES_REQUESTED"}, "source_pr_not_approved"),
        ({"checks_passed": False}, "source_pr_checks_failed"),
        ({"base_ref": "main"}, "source_pr_base_mismatch"),
    ),
)
def test_promote_requires_reviewed_checked_staging_source(
    promotion_harness: PromotionHarness,
    source_change: dict[str, object],
    blocker: str,
) -> None:
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372], **source_change
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == blocker
    assert not promotion_harness.registry.db_path.exists()


def test_promote_verifies_before_pushing_or_creating_a_pr(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(
        verify_production=(
            (
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('verification failed'); sys.exit(7)",
            ),
        )
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert promotion_harness.github.create_calls == []
    assert (
        git_command(
            promotion_harness.repo,
            "ls-remote",
            "--heads",
            "origin",
            "awf/pr-372-to-main/promote",
        )
        == ""
    )


def test_promote_reconciles_existing_pr_after_registry_transition_failure(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_transition = promotion_harness.registry.transition

    def fail_pr_open_transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "promotion_pr_open":
            raise RuntimeError("registry temporarily unavailable")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        promotion_harness.registry, "transition", fail_pr_open_transition
    )
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    monkeypatch.setattr(promotion_harness.registry, "transition", original_transition)

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert second.lease.target_pr == 900
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_rejects_target_pr_with_a_different_head(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.github.created_head_sha = "unreviewed-remote-head"

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "target_pr_head_mismatch"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED


def test_promote_never_reconciles_a_conflicted_blocked_lease(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert first.lease is not None
    promotion_harness.github.open_prs[(first.lease.branch, "main")] = PullRequest(
        number=901,
        state="OPEN",
        base_ref="main",
        base_sha="target-base",
        head_ref=first.lease.branch,
        head_sha=promotion_harness.git.head_sha(first.lease.worktree_path),
        merge_commit_sha=None,
        review_decision="",
        checks_passed=True,
        changed_paths=(),
        url="https://github.example/acme/repo/pull/901",
    )

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.lease is not None
    assert second.lease.state is LeaseState.BLOCKED
    assert second.lease.target_pr is None
    assert promotion_harness.github.find_calls == []


def test_promote_retries_publish_after_a_transient_push_failure(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_push = promotion_harness.git.push_branch

    def fail_push(*_: object) -> None:
        raise GitError("transient remote failure")

    monkeypatch.setattr(promotion_harness.git, "push_branch", fail_push)
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    monkeypatch.setattr(promotion_harness.git, "push_branch", original_push)

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.lease is not None
    assert first.lease.state is LeaseState.ACTIVE
    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_applies_aggregate_delta_containing_a_merge_commit(
    tmp_path: Path,
) -> None:
    harness = PromotionHarness.create(tmp_path, aggregate=True)

    result = harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert harness.git.changed_paths(
        result.lease.worktree_path, result.lease.base_ref
    ) == ("feature.txt", "support.txt")


def test_promote_recovers_when_pending_transition_fails(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_transition = promotion_harness.registry.transition

    def fail_pending_transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "promotion_publish_pending":
            raise RuntimeError("registry temporarily unavailable")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        promotion_harness.registry, "transition", fail_pending_transition
    )
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    monkeypatch.setattr(promotion_harness.registry, "transition", original_transition)

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.lease is not None
    assert first.lease.state is LeaseState.ACTIVE
    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_recovers_pending_worktree_after_source_base_advances(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_transition = promotion_harness.registry.transition

    def fail_pending_transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "promotion_publish_pending":
            raise RuntimeError("registry temporarily unavailable")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        promotion_harness.registry, "transition", fail_pending_transition
    )
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    monkeypatch.setattr(promotion_harness.registry, "transition", original_transition)
    (promotion_harness.repo / "later-team.txt").write_text("later\n", encoding="utf-8")
    git_command(promotion_harness.repo, "add", "later-team.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "later staging change")
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372],
        base_sha=promotion_harness.git.head_sha(promotion_harness.repo),
    )

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN


@pytest.mark.parametrize("field", ("base_sha", "head_sha", "merge_commit_sha"))
def test_promote_rejects_invalid_github_oids_before_git_mutation(
    promotion_harness: PromotionHarness, field: str
) -> None:
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372], **{field: "--malformed"}
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_invalid_oid"
    assert not promotion_harness.registry.db_path.exists()


def test_production_verifier_bounds_and_redacts_large_stderr(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(
        verify_production=(
            (
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('token secret-value ' + 'x' * 1_000_000); sys.exit(7)",
            ),
        )
    )

    with pytest.raises(RuntimeError) as error:
        promotion_harness.service._verify_promotion(promotion_harness.repo)

    detail = str(error.value)
    assert "secret-value" not in detail
    assert "<redacted>" in detail
    assert len(detail.encode("utf-8")) <= 600


def test_production_verifier_kills_timeout_descendant(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.worktrees import service as service_module

    pid_path = tmp_path / "descendant.pid"
    monkeypatch.setattr(service_module, "_PRODUCTION_VERIFY_TIMEOUT_SECONDS", 0.1)
    script = (
        "import os, pathlib, signal, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'], "
        "stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", script),)
    )

    with pytest.raises(RuntimeError, match="timed out"):
        promotion_harness.service._verify_promotion(promotion_harness.repo)
    child_pid = int(pid_path.read_text(encoding="utf-8"))

    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(child_pid)],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not status or status.startswith("Z")