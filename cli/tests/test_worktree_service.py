from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import awf.worktrees.service as service_module
from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient, GitError, GitRemoteError, GitWorktree
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
    create_error: ExternalServiceError | None = None
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
        if self.create_error is not None:
            raise self.create_error
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
    def merged_feature(self, *, age_days: int, initiative: str = "merged") -> Lease:
        acquired = self.acquire(initiative)
        assert acquired.lease is not None
        attached = self.attach_pr(acquired.lease, 1_000 + len(self.github.prs))
        head_sha = self.git.head_sha(attached.worktree_path)
        self.github.prs[attached.target_pr] = merged_pr(
            number=attached.target_pr,
            head_sha=head_sha,
        )
        self.service.status(refresh=True)
        stale_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(
            timespec="seconds"
        )
        with sqlite3.connect(self.registry.db_path) as connection:
            connection.execute(
                """
                UPDATE worktree_leases
                SET created_at = ?, last_used_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (stale_at, stale_at, stale_at, attached.id),
            )
        lease = self.registry.get_lease(attached.id)
        assert lease is not None
        return lease


def test_default_skill_source_uses_installable_package_resource(
    harness: Harness,
) -> None:
    expected = (
        Path(service_module.__file__).resolve().parents[1]
        / "resources"
        / "release-worktree-lifecycle"
    ).resolve()

    assert harness.service.skill_source_dir == expected
    assert (expected / "SKILL.md").is_file()
    repository_source = (
        Path(__file__).resolve().parents[2]
        / "claude"
        / "skills"
        / "release-worktree-lifecycle"
        / "SKILL.md"
    )
    assert (expected / "SKILL.md").read_bytes() == repository_source.read_bytes()


def matching_adoption_pr(
    harness: Harness, lease: Lease, number: int = 129
) -> PullRequest:
    return replace(
        merged_pr(number=number, head_sha=lease.head_sha),
        head_ref=lease.branch,
    )


def developed_managed_feature(
    harness: Harness, *, number: int = 131
) -> tuple[Lease, str]:
    acquired = harness.acquire("managed-pr-link")
    assert acquired.lease is not None
    lease = acquired.lease
    (lease.worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_command(lease.worktree_path, "add", "feature.txt")
    git_command(lease.worktree_path, "commit", "-q", "-m", "feature")
    current_head = harness.git.head_sha(lease.worktree_path)
    assert current_head != lease.head_sha
    harness.github.prs[number] = replace(
        merged_pr(number=number, head_sha=current_head),
        head_ref=lease.branch,
    )
    return lease, current_head


def adopted_imported_release_worktree(
    harness: Harness, root: Path, *, branch: str = "awf/imported-release"
) -> Lease:
    root.mkdir()
    external = root / "legacy-release"
    git_command(
        harness.repo,
        "worktree",
        "add",
        "-q",
        "-b",
        branch,
        str(external),
        "staging",
    )
    (external / "legacy-release.txt").write_text(
        "legacy release\n", encoding="utf-8"
    )
    git_command(external, "add", "legacy-release.txt")
    git_command(external, "commit", "-q", "-m", "legacy release")
    git_command(external, "push", "-q", "-u", "origin", branch)

    imported_result = harness.service.import_root(root, apply=True)
    imported = next(
        lease for lease in imported_result.leases if lease.worktree_path == external
    )
    harness.github.prs[129] = matching_adoption_pr(harness, imported)
    adopted = harness.service.adopt(imported.id, pr_number=129, apply=True)
    assert adopted.decision == "ready"
    harness.service.status(refresh=True)
    current = harness.registry.get_lease(imported.id)
    assert current is not None
    assert current.state is LeaseState.CLEANABLE
    return current


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

    def add_content_mismatch_source(self, number: int = 373) -> PullRequest:
        git_command(self.repo, "checkout", "-q", "main")
        (self.repo / "feature.txt").write_text(
            "copy=old\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n",
            encoding="utf-8",
        )
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "production prerequisite old")
        git_command(self.repo, "push", "-q", "origin", "main")

        git_command(self.repo, "checkout", "-q", "staging")
        (self.repo / "feature.txt").write_text(
            "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n",
            encoding="utf-8",
        )
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "staging prerequisite")
        git_command(self.repo, "push", "-q", "origin", "staging")
        return self.add_followup_source(
            number,
            feature_text=(
                "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=new\n"
            ),
            change_feature=True,
            include_followup=False,
        )

    def advance_target_prerequisite(self) -> str:
        git_command(self.repo, "checkout", "-q", "main")
        (self.repo / "feature.txt").write_text(
            "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n",
            encoding="utf-8",
        )
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "land production prerequisite")
        target_sha = git_command(self.repo, "rev-parse", "HEAD")
        git_command(self.repo, "push", "-q", "origin", "main")
        git_command(self.repo, "checkout", "-q", "staging")
        return target_sha


    def add_followup_source(
        self,
        number: int = 373,
        *,
        feature_text: str | None = "feature updated\n",
        change_feature: bool = True,
        include_followup: bool = True,
        team_text: str | None = None,
    ) -> PullRequest:
        git_command(self.repo, "checkout", "-q", "staging")
        base_sha = git_command(self.repo, "rev-parse", "HEAD")
        branch = f"feature/pr-{number}"
        git_command(self.repo, "checkout", "-q", "-b", branch)
        changed_paths: list[str] = []
        if change_feature:
            if feature_text is None:
                (self.repo / "feature.txt").unlink()
            else:
                (self.repo / "feature.txt").write_text(
                    feature_text, encoding="utf-8"
                )
            changed_paths.append("feature.txt")
        if include_followup:
            (self.repo / "followup.txt").write_text(
                "followup\n", encoding="utf-8"
            )
            changed_paths.append("followup.txt")
        if team_text is not None:
            (self.repo / "team.txt").write_text(team_text, encoding="utf-8")
            changed_paths.append("team.txt")
        git_command(self.repo, "add", "-A", *changed_paths)
        git_command(self.repo, "commit", "-q", "-m", "source followup")
        head_sha = git_command(self.repo, "rev-parse", "HEAD")
        git_command(self.repo, "push", "-q", "-u", "origin", branch)
        git_command(self.repo, "checkout", "-q", "staging")
        git_command(
            self.repo,
            "merge",
            "--no-ff",
            "-q",
            branch,
            "-m",
            "merge source followup",
        )
        merge_sha = git_command(self.repo, "rev-parse", "HEAD")
        git_command(self.repo, "push", "-q", "origin", "staging")
        source = PullRequest(
            number=number,
            state="MERGED",
            base_ref="staging",
            base_sha=base_sha,
            head_ref=branch,
            head_sha=head_sha,
            merge_commit_sha=merge_sha,
            review_decision="APPROVED",
            checks_passed=True,
            changed_paths=tuple(changed_paths),
            url=f"https://github.example/acme/repo/pull/{number}",
        )
        self.github.prs[number] = source
        return source

    def merged_promotion(self, mutation: str) -> Lease:
        if mutation == "deployment_unknown":
            self.configure(deployment_status_command=())
        elif mutation == "deployment_failed":
            self.configure(
                deployment_status_command=(
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(1)",
                )
            )
        else:
            self.configure(
                deployment_status_command=(sys.executable, "-c", "pass")
            )
        promoted = self.service.promote(
            source_pr=372, target_branch="main", apply=True
        )
        assert promoted.lease is not None
        target_pr = promoted.lease.target_pr
        assert target_pr is not None
        target = self.github.prs[target_pr]
        self.github.prs[target_pr] = replace(
            target,
            state="MERGED",
            head_sha=promoted.lease.head_sha,
            merge_commit_sha=promoted.lease.head_sha,
        )
        self.service.status(refresh=True)
        lease = self.registry.get_lease(promoted.lease.id)
        assert lease is not None
        if mutation == "dirty":
            (lease.worktree_path / "README.txt").write_text(
                "dirty\n", encoding="utf-8"
            )
        elif mutation == "untracked":
            (lease.worktree_path / "local.txt").write_text(
                "keep\n", encoding="utf-8"
            )
        elif mutation == "closed":
            self.github.prs[target_pr] = replace(
                self.github.prs[target_pr], state="CLOSED", merge_commit_sha=None
            )
        elif mutation == "head_mismatch":
            self.github.prs[target_pr] = replace(
                self.github.prs[target_pr], head_sha="0" * 40
            )
        elif mutation == "unmanaged":
            lease = self.registry.transition(
                lease.id,
                lease.state,
                expected_version=lease.version,
                managed=False,
            )
        elif mutation == "retain":
            lease = self.registry.transition(
                lease.id,
                lease.state,
                expected_version=lease.version,
                retain=True,
            )
        return lease
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
                    "author": {"login": "source-author"},
                    "mergedBy": {"login": "source-merger"},
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
                    "mergeCommit,reviewDecision,statusCheckRollup,files,url,author,mergedBy"
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
    assert pr.author_login == "source-author"
    assert pr.merged_by_login == "source-merger"


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


def test_refresh_blocks_merged_feature_when_pr_head_differs_from_lease(
    harness: Harness,
) -> None:
    acquired = harness.acquire("reward-widget")
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    pull_request_head = "different-head-sha"
    harness.github.prs[42] = merged_pr(
        number=42,
        base="staging",
        head_sha=pull_request_head,
        changed_paths=("README.txt",),
    )

    result = harness.service.status(initiative="reward-widget", refresh=True)

    current = result.leases[0]
    assert current.state is LeaseState.BLOCKED
    assert current.deployment_state is DeploymentState.UNKNOWN
    assert current.head_sha == lease.head_sha
    event = harness.registry.list_events(lease.id)[-1]
    assert event.event_type == "github_refresh_head_mismatch"
    assert event.observed_head_sha == pull_request_head
    assert event.pr_number == 42
    assert event.summary == (
        "GitHub refresh: pull request HEAD does not match recorded lease HEAD"
    )
    payload = result.to_dict()
    assert payload["leases"][0]["state"] == "BLOCKED"
    assert payload["leases"][0]["deployment_state"] == "unknown"


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


def test_refresh_blocks_merged_promotion_when_pr_head_differs_before_deployment(
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
    pull_request_head = "different-head-sha"
    harness.github.prs[42] = merged_pr(number=42, head_sha=pull_request_head)
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

    result = harness.service.status(initiative="reward-widget", refresh=True)

    current = result.leases[0]
    assert current.state is LeaseState.BLOCKED
    assert current.deployment_state is DeploymentState.UNKNOWN
    assert current.head_sha == lease.head_sha
    assert deployment_calls == []
    event = harness.registry.list_events(lease.id)[-1]
    assert event.event_type == "github_refresh_head_mismatch"
    assert event.observed_head_sha == pull_request_head
    assert event.pr_number == 42
    payload = result.to_dict()
    assert payload["leases"][0]["state"] == "BLOCKED"
    assert payload["leases"][0]["deployment_state"] == "unknown"


def test_refresh_records_head_mismatch_for_unrelated_blocked_promotion(
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
    unrelated_block = harness.registry.transition(
        lease.id,
        LeaseState.BLOCKED,
        expected_version=lease.version,
        event_type="deployment_refresh_failed",
        summary="unrelated deployment failure",
        deployment_state=DeploymentState.UNKNOWN,
    )
    pull_request_head = "different-head-sha"
    harness.github.prs[42] = merged_pr(number=42, head_sha=pull_request_head)
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
    events = harness.registry.list_events(lease.id)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.BLOCKED
    assert first.leases[0].deployment_state is DeploymentState.UNKNOWN
    assert first.leases[0].version == unrelated_block.version + 1
    assert events[-1].event_type == "github_refresh_head_mismatch"
    assert events[-1].observed_head_sha == pull_request_head
    assert deployment_calls == []
    assert second.leases[0].version == first.leases[0].version
    assert harness.registry.list_events(lease.id) == events


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


@pytest.mark.parametrize("marker_key", [None, "stale-prepare-key"])
def test_acquire_preview_reuses_active_lease_without_mutation_or_preparing(
    harness: Harness, marker_key: str | None, monkeypatch
) -> None:
    first = harness.acquire("reward-widget")
    assert first.lease is not None
    marker = harness.state_dir / "prepare" / f"{first.lease.id}.json"
    if marker_key is not None:
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"key": marker_key}), encoding="utf-8")
    list_worktree_calls: list[None] = []
    status_paths: list[Path | None] = []
    head_paths: list[Path | None] = []
    list_worktrees = harness.git.list_worktrees
    status_porcelain = harness.git.status_porcelain
    head_sha = harness.git.head_sha

    def recording_list_worktrees():
        list_worktree_calls.append(None)
        return list_worktrees()

    def recording_status_porcelain(cwd: Path | None = None):
        status_paths.append(cwd)
        return status_porcelain(cwd)

    def recording_head_sha(cwd: Path | None = None):
        head_paths.append(cwd)
        return head_sha(cwd)

    monkeypatch.setattr(harness.git, "list_worktrees", recording_list_worktrees)
    monkeypatch.setattr(harness.git, "status_porcelain", recording_status_porcelain)
    monkeypatch.setattr(harness.git, "head_sha", recording_head_sha)


    runner_calls: list[tuple[object, ...]] = []

    def failing_prepare_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        runner_calls.append(args)
        return subprocess.CompletedProcess(args[0], 7, "", "prepare failed")

    harness.service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(
            default_base="staging",
            prepare_command=("prepare",),
        ),
        command_runner=failing_prepare_runner,
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
    )
    before = harness.registry.get_lease(first.lease.id)
    assert before is not None
    before_events = harness.registry.list_events(first.lease.id)

    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=False,
    )

    after = harness.registry.get_lease(first.lease.id)
    assert result.status == "ok"
    assert result.decision == "reuse"
    assert result.lease == before
    assert after == before
    assert after.version == before.version
    assert after.last_used_at == before.last_used_at
    assert after.head_sha == before.head_sha
    assert harness.registry.list_events(first.lease.id) == before_events
    assert list_worktree_calls == [None]
    assert status_paths == [first.lease.worktree_path]
    assert head_paths == [first.lease.worktree_path]
    assert runner_calls == []
    if marker_key is None:
        assert not marker.exists()
    else:
        assert json.loads(marker.read_text(encoding="utf-8")) == {"key": marker_key}


@pytest.mark.parametrize("lock_exists", [False, True])
def test_acquire_preview_reuse_does_not_mutate_the_repository_lock(
    harness: Harness, lock_exists: bool, monkeypatch
) -> None:
    first = harness.acquire("reward-widget")
    assert first.lease is not None
    lock_path = harness.lock_dir / f"{harness.git.repository_id()}.lock"
    assert lock_path.is_file()
    if lock_exists:
        lock_path.write_bytes(b"pre-existing lock content\n")
        os.utime(lock_path, ns=(1_700_000_000_000_000_000,) * 2)
        before_lock = (lock_path.read_bytes(), lock_path.stat().st_mtime_ns)
    else:
        lock_path.unlink()

    snapshot_calls: list[dict[str, object]] = []
    list_leases_read_only = harness.registry.list_leases_read_only

    def recording_list_leases_read_only(**kwargs: object) -> list[Lease]:
        snapshot_calls.append(kwargs)
        return list_leases_read_only(**kwargs)

    monkeypatch.setattr(
        harness.registry, "list_leases_read_only", recording_list_leases_read_only
    )

    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=False,
    )

    assert result.status == "ok"
    assert result.decision == "reuse"
    assert snapshot_calls == [
        {
            "include_removed": False,
            "repository_id": harness.git.repository_id(),
            "initiative": "reward-widget",
            "purpose": Purpose.FEATURE,
        }
    ]
    if lock_exists:
        assert (lock_path.read_bytes(), lock_path.stat().st_mtime_ns) == before_lock
    else:
        assert not lock_path.exists()

@pytest.mark.parametrize("apply", [False, True])
def test_acquire_blocks_when_registered_path_is_missing(
    harness: Harness, apply: bool
) -> None:
    first = harness.acquire("reward-widget")
    assert first.lease is not None
    subprocess.run(
        ["git", "worktree", "remove", str(first.lease.worktree_path)],
        cwd=harness.repo,
        check=True,
    )

    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=apply,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "orphaned_lease"


def test_acquire_preview_blocks_a_dirty_active_lease(harness: Harness) -> None:
    first = harness.acquire("reward-widget")
    assert first.lease is not None
    (first.lease.worktree_path / "uncommitted.txt").write_text(
        "uncommitted\n", encoding="utf-8"
    )

    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "dirty_lease"


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


@pytest.mark.parametrize("apply", [False, True])
def test_acquire_blocks_for_a_conflicting_requested_branch(
    harness: Harness, apply: bool
) -> None:
    harness.acquire("reward-widget")

    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch="awf/reward-widget/alternate",
        owner_id="session-1",
        apply=apply,
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


def test_adopt_pr_refuses_dirty_imported_worktree(harness: Harness) -> None:
    imported = harness.import_external("legacy-release")
    harness.github.prs[129] = matching_adoption_pr(harness, imported)
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)

    result = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "dirty_worktree"
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == []


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


def test_adopt_preview_links_matching_merged_pr_without_mutation(
    harness: Harness,
) -> None:
    external = harness.make_external_worktree("legacy-release")
    imported = harness.registry.create_lease(
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
    harness.github.prs[129] = PullRequest(
        number=129,
        state="MERGED",
        base_ref="staging",
        base_sha=harness.git.head_sha(),
        head_ref=imported.branch,
        head_sha=imported.head_sha,
        merge_commit_sha=harness.git.head_sha(),
        review_decision="APPROVED",
        checks_passed=True,
        changed_paths=(),
        url="https://github.example/acme/repo/pull/129",
    )
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_version = before.version
    before_events = harness.registry.list_events(imported.id)
    assert not harness.lock_dir.exists()

    result = harness.service.adopt(imported.id, pr_number=129, apply=False)

    assert result.decision == "preview"
    assert result.actions == (
        {
            "kind": "link_pr",
            "lease_id": imported.id,
            "path": str(imported.worktree_path),
            "pr_number": 129,
            "head_sha": imported.head_sha,
        },
    )
    after = harness.registry.get_lease(imported.id)
    assert after == before
    assert after.version == before_version
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == [129]
    assert not harness.lock_dir.exists()


@pytest.mark.parametrize("pr_state", ("MERGED", "CLOSED"))
def test_adopt_links_matching_completed_pr_atomically(
    harness: Harness, pr_state: str
) -> None:
    external = harness.make_external_worktree("legacy-release")
    imported = harness.registry.create_lease(
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
    harness.github.prs[129] = PullRequest(
        number=129,
        state=pr_state,
        base_ref="staging",
        base_sha=harness.git.head_sha(),
        head_ref=imported.branch,
        head_sha=imported.head_sha,
        merge_commit_sha=harness.git.head_sha(),
        review_decision="APPROVED",
        checks_passed=True,
        changed_paths=(),
        url="https://github.example/acme/repo/pull/129",
    )

    result = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.managed is True
    assert result.lease.target_pr == 129
    assert result.lease.state is imported.state
    stored = harness.registry.get_lease(imported.id)
    assert stored == result.lease
    event = harness.registry.list_events(imported.id)[-1]
    assert event.event_type == "imported_lease_pr_linked"
    assert event.from_state is imported.state
    assert event.to_state is imported.state
    assert event.observed_head_sha == imported.head_sha
    assert event.pr_number == 129
    assert harness.github.view_calls == [129, 129]


@pytest.mark.parametrize(
    ("pr_number", "failure", "expected_code", "expected_calls"),
    (
        (0, "invalid_number", "invalid_pr_number", ()),
        (-1, "invalid_number", "invalid_pr_number", ()),
        (True, "invalid_number", "invalid_pr_number", ()),
        (129, "open", "pr_not_merged", (129,)),
        (129, "closed_without_merge", "pr_not_merged", (129,)),
        (129, "number_mismatch", "pr_number_mismatch", (129,)),
        (129, "branch_mismatch", "pr_branch_mismatch", (129,)),
        (129, "head_mismatch", "pr_head_mismatch", (129,)),
    ),
)
def test_adopt_pr_failure_matrix_leaves_imported_lease_unchanged(
    harness: Harness,
    pr_number: int,
    failure: str,
    expected_code: str,
    expected_calls: tuple[int, ...],
) -> None:
    imported = harness.import_external("legacy-release")
    matching = matching_adoption_pr(harness, imported)
    if failure == "open":
        harness.github.prs[129] = replace(
            matching, state="OPEN", merge_commit_sha=None
        )
    elif failure == "closed_without_merge":
        harness.github.prs[129] = replace(
            matching, state="CLOSED", merge_commit_sha=None
        )
    elif failure == "number_mismatch":
        harness.github.prs[129] = replace(matching, number=130)
    elif failure == "branch_mismatch":
        harness.github.prs[129] = replace(matching, head_ref="other-branch")
    elif failure == "head_mismatch":
        harness.github.prs[129] = replace(matching, head_sha="0" * 40)
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)

    result = harness.service.adopt(imported.id, pr_number=pr_number, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == expected_code
    stored = harness.registry.get_lease(imported.id)
    assert stored == before
    assert stored.managed is False
    assert stored.version == before.version
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == list(expected_calls)


def test_adopt_pr_provider_failure_is_external_and_leaves_no_mutation(
    harness: Harness,
) -> None:
    imported = harness.import_external("legacy-release")
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)
    harness.github.error = ExternalServiceError("gh auth required")

    result = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert result.command == "wt.adopt"
    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "github_adopt_failed"
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == [129]


@pytest.mark.parametrize(
    ("safety_failure", "expected_code"),
    (
        ("repository", "repository_mismatch"),
        ("orphaned", "orphaned_lease"),
        ("detached", "branch_mismatch"),
        ("branch", "branch_mismatch"),
        ("head", "head_mismatch"),
    ),
)
def test_adopt_pr_preserves_imported_git_safety_blockers(
    tmp_path: Path,
    safety_failure: str,
    expected_code: str,
) -> None:
    worktree_path = tmp_path / "registered"
    worktree_path.mkdir()
    head_sha = "1234567890abcdef1234567890abcdef12345678"

    class FakeGit:
        def repository_id(self) -> str:
            return (
                "other-repository"
                if safety_failure == "repository"
                else "repository-id"
            )

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            if safety_failure == "orphaned":
                return ()
            if safety_failure == "detached":
                return (
                    GitWorktree(
                        worktree_path, head_sha, None, detached=True
                    ),
                )
            if safety_failure == "branch":
                return (
                    GitWorktree(worktree_path, head_sha, "other-branch"),
                )
            if safety_failure == "head":
                return (
                    GitWorktree(
                        worktree_path, "0" * 40, "topic/release"
                    ),
                )
            return (GitWorktree(worktree_path, head_sha, "topic/release"),)

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
    github = FakeGitHub()
    service = WorktreeService(
        registry,
        FakeGit(),
        config=WorktreeConfig(),
        github=github,
        lock_dir=tmp_path / "locks",
    )
    before_events = registry.list_events(lease.id)

    result = service.adopt(lease.id, pr_number=129, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == expected_code
    assert registry.get_lease(lease.id) == lease
    assert registry.list_events(lease.id) == before_events
    assert github.view_calls == []


def test_adopt_pr_reuses_exact_link_without_registry_mutation(
    harness: Harness,
) -> None:
    imported = harness.import_external("legacy-release")
    harness.github.prs[129] = matching_adoption_pr(harness, imported)

    adopted = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert adopted.decision == "ready"
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)
    lock_path = harness.lock_dir / f"{before.repository_id}.lock"
    assert lock_path.is_file()
    lock_path.unlink()
    harness.lock_dir.rmdir()

    preview = harness.service.adopt(imported.id, pr_number=129, apply=False)

    assert preview.decision == "reuse"
    assert preview.lease == before
    assert not harness.lock_dir.exists()
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events

    applied = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert applied.decision == "reuse"
    assert applied.lease == before
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == [129, 129, 129, 129, 129]
    assert lock_path.is_file()


def test_adopt_pr_rejects_different_link_without_provider_inference(
    harness: Harness,
) -> None:
    imported = harness.import_external("legacy-release")
    harness.github.prs[129] = matching_adoption_pr(harness, imported)
    adopted = harness.service.adopt(imported.id, pr_number=129, apply=True)
    assert adopted.decision == "ready"
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)

    result = harness.service.adopt(imported.id, pr_number=130, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "pr_link_mismatch"
    assert result.lease == before
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == [129, 129]


def test_adopt_without_pr_still_blocks_a_second_adoption(
    harness: Harness,
) -> None:
    imported = harness.import_external("legacy-release")
    adopted = harness.service.adopt(imported.id, apply=True)
    assert adopted.decision == "ready"
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)

    result = harness.service.adopt(imported.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "already_adopted"
    assert result.lease == before
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events


def test_adopt_pr_apply_revalidates_provider_inside_repository_lock(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = harness.import_external("legacy-release")
    matching = matching_adoption_pr(harness, imported)
    mismatched = replace(matching, head_sha="0" * 40)
    view_calls: list[int] = []

    def view_pr(number: int) -> PullRequest:
        view_calls.append(number)
        return matching if len(view_calls) == 1 else mismatched

    monkeypatch.setattr(harness.github, "view_pr", view_pr)
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)

    result = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "pr_head_mismatch"
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events
    assert view_calls == [129, 129]


@pytest.mark.parametrize(
    "already_linked",
    (False, True),
    ids=("initial_adoption", "exact_link_reuse"),
)
def test_adopt_pr_apply_revalidates_git_after_locked_provider_call(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    already_linked: bool,
) -> None:
    imported = harness.import_external("legacy-release")
    matching = matching_adoption_pr(harness, imported)
    harness.github.prs[129] = matching
    if already_linked:
        adopted = harness.service.adopt(imported.id, pr_number=129, apply=True)
        assert adopted.decision == "ready"
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)
    view_calls: list[int] = []

    def view_pr(number: int) -> PullRequest:
        view_calls.append(number)
        if len(view_calls) == 2:
            git_command(
                imported.worktree_path,
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "mutate HEAD during provider validation",
            )
        return matching

    monkeypatch.setattr(harness.github, "view_pr", view_pr)

    result = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert result.status == "blocked"
    assert result.decision == "blocked"
    assert result.blockers[0]["code"] == "head_mismatch"
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events
    assert view_calls == [129, 129]

@pytest.mark.parametrize(
    "error_type",
    (RuntimeError, sqlite3.OperationalError),
    ids=("runtime_error", "sqlite_error"),
)
@pytest.mark.parametrize(
    "pr_number",
    (None, 129),
    ids=("without_pr", "with_pr"),
)
def test_adopt_apply_reports_registry_transition_conflicts(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    pr_number: int | None,
    error_type: type[Exception],
) -> None:
    imported = harness.import_external("legacy-release")
    if pr_number is not None:
        harness.github.prs[pr_number] = replace(
            merged_pr(number=pr_number, head_sha=imported.head_sha),
            head_ref=imported.branch,
        )
    before = harness.registry.get_lease(imported.id)
    assert before is not None
    before_events = harness.registry.list_events(imported.id)

    def fail_transition(*args: object, **kwargs: object) -> Lease:
        raise error_type("registry temporarily unavailable")

    monkeypatch.setattr(harness.registry, "transition", fail_transition)
    keyword_args = {} if pr_number is None else {"pr_number": pr_number}

    result = harness.service.adopt(imported.id, apply=True, **keyword_args)

    assert result.command == "wt.adopt"
    assert result.status == "error"
    assert result.exit_code == 5
    assert result.blockers[0]["code"] == "registry_conflict"
    assert result.lease == before
    assert harness.registry.get_lease(imported.id) == before
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == ([] if pr_number is None else [pr_number, pr_number])


def test_link_pr_preview_validates_developed_head_without_mutation(
    harness: Harness,
) -> None:
    lease, current_head = developed_managed_feature(harness)
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=False)

    assert result.decision == "preview"
    assert result.actions == (
        {
            "kind": "link_pr",
            "lease_id": lease.id,
            "path": str(lease.worktree_path),
            "pr_number": 131,
            "head_sha": current_head,
        },
    )
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == [131]


def test_link_pr_records_verified_head_and_cleanup_state_atomically(
    harness: Harness,
) -> None:
    lease, current_head = developed_managed_feature(harness)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.target_pr == 131
    assert result.lease.head_sha == current_head
    assert result.lease.state is LeaseState.CLEANABLE
    assert result.lease.deployment_state is DeploymentState.NOT_REQUIRED
    assert harness.registry.get_lease(lease.id) == result.lease
    event = harness.registry.list_events(lease.id)[-1]
    assert event.event_type == "managed_lease_pr_linked"
    assert event.observed_head_sha == current_head
    assert event.pr_number == 131
    assert harness.github.view_calls == [131, 131]


def test_link_pr_reuses_exact_link_without_registry_mutation(
    harness: Harness,
) -> None:
    lease, _ = developed_managed_feature(harness)
    linked = harness.service.link_pr(lease.id, pr_number=131, apply=True)
    assert linked.decision == "ready"
    before = harness.registry.get_lease(lease.id)
    assert before is not None
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.decision == "reuse"
    assert result.lease == before
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events


def test_link_pr_rejects_different_link_without_provider_inference(
    harness: Harness,
) -> None:
    lease, current_head = developed_managed_feature(harness)
    linked = harness.service.link_pr(lease.id, pr_number=131, apply=True)
    assert linked.decision == "ready"
    harness.github.prs[132] = replace(
        merged_pr(number=132, head_sha=current_head),
        head_ref=lease.branch,
    )
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=132, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "pr_link_mismatch"
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == [131, 131]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("open", "pr_not_merged"),
        ("closed_unmerged", "pr_not_merged"),
        ("number", "pr_number_mismatch"),
        ("branch", "pr_branch_mismatch"),
        ("head", "pr_head_mismatch"),
        ("dirty", "dirty_worktree"),
    ),
)
def test_link_pr_rejects_unproven_links_without_mutation(
    harness: Harness, mutation: str, expected_code: str
) -> None:
    lease, current_head = developed_managed_feature(harness)
    matching = harness.github.prs[131]
    if mutation == "open":
        harness.github.prs[131] = replace(
            matching, state="OPEN", merge_commit_sha=None
        )
    elif mutation == "closed_unmerged":
        harness.github.prs[131] = replace(
            matching, state="CLOSED", merge_commit_sha=None
        )
    elif mutation == "number":
        harness.github.prs[131] = replace(matching, number=132)
    elif mutation == "branch":
        harness.github.prs[131] = replace(matching, head_ref="other-branch")
    elif mutation == "head":
        harness.github.prs[131] = replace(matching, head_sha="0" * 40)
    elif mutation == "dirty":
        (lease.worktree_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == expected_code
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.git.head_sha(lease.worktree_path) == current_head


def test_link_pr_accepts_closed_pr_with_merge_commit(
    harness: Harness,
) -> None:
    lease, _ = developed_managed_feature(harness)
    harness.github.prs[131] = replace(
        harness.github.prs[131],
        state="CLOSED",
        merge_commit_sha="f" * 40,
    )

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.state is LeaseState.CLEANABLE
    assert result.lease.target_pr == 131


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    (
        ("repository", "repository_mismatch"),
        ("orphaned", "orphaned_lease"),
        ("detached", "branch_mismatch"),
    ),
)
def test_link_pr_preserves_managed_git_safety_blockers(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_code: str,
) -> None:
    lease, _ = developed_managed_feature(harness)
    registered = harness.git.list_worktrees()
    target = next(item for item in registered if item.path == lease.worktree_path)
    if failure == "repository":
        monkeypatch.setattr(
            harness.git, "repository_id", lambda: "other-repository"
        )
    elif failure == "orphaned":
        monkeypatch.setattr(
            harness.git,
            "list_worktrees",
            lambda: tuple(item for item in registered if item is not target),
        )
    else:
        monkeypatch.setattr(
            harness.git,
            "list_worktrees",
            lambda: tuple(
                replace(item, branch=None, detached=True)
                if item is target
                else item
                for item in registered
            ),
        )
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == expected_code
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == []


def test_link_pr_rejects_removed_managed_feature_lease(
    harness: Harness,
) -> None:
    lease, _ = developed_managed_feature(harness)
    removed = harness.registry.transition(
        lease.id,
        LeaseState.REMOVED,
        expected_version=lease.version,
        event_type="removed",
        summary="removed",
    )
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "removed_lease"
    assert harness.registry.get_lease(lease.id) == removed
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == []


def test_link_pr_rejects_unmanaged_imported_lease(
    harness: Harness,
) -> None:
    imported = harness.import_external("legacy-release")
    before_events = harness.registry.list_events(imported.id)

    result = harness.service.link_pr(imported.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "unmanaged_lease"
    assert harness.registry.get_lease(imported.id) == imported
    assert harness.registry.list_events(imported.id) == before_events
    assert harness.github.view_calls == []


def test_link_pr_rejects_non_feature_managed_lease(
    harness: Harness,
) -> None:
    worktree_path = harness.make_external_worktree("promotion-link")
    lease = harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.git.repository_root(),
            worktree_path=worktree_path,
            initiative="promotion-link",
            purpose=Purpose.PROMOTE,
            branch="promotion-link",
            base_ref="staging",
            head_sha=harness.git.head_sha(worktree_path),
            managed=True,
            owner_kind="awf",
        )
    )
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "unsupported_purpose"
    assert harness.registry.get_lease(lease.id) == lease
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == []


def test_link_pr_rejects_blocked_managed_feature_lease(
    harness: Harness,
) -> None:
    lease, _ = developed_managed_feature(harness)
    blocked = harness.registry.transition(
        lease.id,
        LeaseState.BLOCKED,
        expected_version=lease.version,
        event_type="prepare_failed",
        summary="prepare failed",
    )
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "unsupported_state"
    assert harness.registry.get_lease(lease.id) == blocked
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == []


def test_link_pr_provider_failure_is_external_and_leaves_no_mutation(
    harness: Harness,
) -> None:
    lease, _ = developed_managed_feature(harness)
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)
    harness.github.error = ExternalServiceError("gh auth required")

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.command == "wt.link-pr"
    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "github_link_failed"
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == [131]


def test_link_pr_refuses_cleanup_reserved_lease(
    harness: Harness,
) -> None:
    lease, _ = developed_managed_feature(harness)
    harness.registry.reserve_cleanup(
        lease.id,
        expected_version=lease.version,
        branch_sha=lease.head_sha,
    )
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "cleanup_reserved"
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.github.view_calls == []


def test_link_pr_apply_revalidates_git_after_locked_provider_call(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, _ = developed_managed_feature(harness)
    matching = harness.github.prs[131]
    view_calls: list[int] = []

    def view_pr(number: int) -> PullRequest:
        view_calls.append(number)
        if len(view_calls) == 2:
            git_command(
                lease.worktree_path,
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "mutate HEAD during provider validation",
            )
        return matching

    monkeypatch.setattr(harness.github, "view_pr", view_pr)
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "pr_head_mismatch"
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert view_calls == [131, 131]


def test_link_pr_enables_finish_cleanup_end_to_end(
    harness: Harness,
) -> None:
    lease, _ = developed_managed_feature(harness)
    linked = harness.service.link_pr(lease.id, pr_number=131, apply=True)
    assert linked.decision == "ready"

    result = harness.service.finish(pr_number=131, apply=False)

    assert result.decision == "preview"
    assert result.blockers == ()
    assert result.actions == (
        {
            "kind": "remove_worktree",
            "lease_id": lease.id,
            "path": str(lease.worktree_path),
            "branch": lease.branch,
        },
    )

    removed = harness.service.finish(pr_number=131, apply=True)

    assert removed.decision == "removed"
    stored = harness.registry.get_lease(lease.id)
    assert stored is not None
    assert stored.state is LeaseState.REMOVED
    assert not lease.worktree_path.exists()

    doctor = harness.service.doctor()

    assert all(
        action.get("lease_id") != lease.id
        and action.get("path") != str(lease.worktree_path)
        for action in doctor.actions
    )


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
    github = FakeGitHub()
    service = WorktreeService(
        registry,
        FakeGit(),
        config=WorktreeConfig(),
        github=github,
        lock_dir=tmp_path / "locks",
    )
    before_events = registry.list_events(lease.id)

    result = service.adopt(lease.id, pr_number=129, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "branch_conflict"
    assert registry.get_lease(lease.id).managed is False
    assert registry.list_events(lease.id) == before_events
    assert github.view_calls == []


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


def test_promote_applies_ordered_multi_source_delta(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.add_followup_source()

    result = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    worktree = result.lease.worktree_path
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature updated\n"
    assert (worktree / "followup.txt").read_text(encoding="utf-8") == "followup\n"
    assert not (worktree / "team.txt").exists()
    assert promotion_harness.git.changed_paths(
        worktree, result.lease.base_ref
    ) == ("feature.txt", "followup.txt")
    assert result.lease.branch == "awf/prs-372-373-to-main/promote"
    body = promotion_harness.github.create_calls[0]["body"]
    assert "AWF-Source-PR: 372" in body
    assert "AWF-Source-PR: 373" in body


def test_promote_accepts_multi_source_delta_with_no_net_path_change(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.add_followup_source(
        feature_text=None,
        include_followup=False,
    )

    result = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    worktree = result.lease.worktree_path
    assert not (worktree / "feature.txt").exists()
    assert promotion_harness.git.changed_paths(
        worktree, result.lease.base_ref
    ) == ()


def test_promote_recovers_multi_source_delta_with_no_net_path_change(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.add_followup_source(
        feature_text=None,
        include_followup=False,
    )
    promotion_harness.configure(
        verify_production=(
            (sys.executable, "-c", "import sys; sys.exit(1)"),
        )
    )

    first = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        apply=True,
    )
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    second = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "promotion_apply_failed"
    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert promotion_harness.github.create_calls[0]["body"].count(
        "AWF-Source-PR:"
    ) == 2


def test_promote_excludes_reviewed_paths_and_recovers_safely(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.add_followup_source(
        change_feature=False,
        include_followup=False,
        team_text="team updated\n",
    )
    promotion_harness.configure(
        verify_production=(
            (sys.executable, "-c", "import sys; sys.exit(1)"),
        )
    )

    first = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        exclude_paths=("team.txt",),
        apply=True,
    )
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    second = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        exclude_paths=("team.txt",),
        apply=True,
    )

    assert first.status == "blocked"
    assert second.decision == "ready"
    assert second.lease is not None
    worktree = second.lease.worktree_path
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert not (worktree / "team.txt").exists()
    assert promotion_harness.git.changed_paths(
        worktree, second.lease.base_ref
    ) == ("feature.txt",)
    assert "-except-" in second.lease.branch
    assert "AWF-Excluded-Path: team.txt" in (
        promotion_harness.github.create_calls[0]["body"]
    )


def test_promote_rejects_reuse_with_different_path_exclusions(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotion_harness.add_followup_source(
        change_feature=False,
        include_followup=False,
        team_text="team updated\n",
    )
    first = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        exclude_paths=("team.txt",),
        apply=True,
    )
    assert first.decision == "ready"
    assert first.lease is not None
    first_initiative = first.lease.initiative
    monkeypatch.setattr(
        WorktreeService,
        "_promotion_initiative",
        staticmethod(
            lambda source_prs, target_branch, excluded_paths=(): first_initiative
        ),
    )

    second = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        exclude_paths=("feature.txt",),
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_lease_conflict"
    assert len(promotion_harness.github.create_calls) == 1


@pytest.mark.parametrize(
    "excluded_paths",
    [
        pytest.param(("missing.txt",), id="unreviewed"),
        pytest.param(("feature.txt",), id="all-reviewed"),
        pytest.param(("/absolute.txt",), id="absolute"),
        pytest.param(("../parent.txt",), id="parent"),
        pytest.param(("team.txt", "team.txt"), id="duplicate"),
        pytest.param(("line\nbreak.txt",), id="newline"),
        pytest.param("feature.txt", id="scalar-string"),
        pytest.param((1,), id="non-string"),
        pytest.param(None, id="none"),
    ],
)
def test_promote_rejects_invalid_excluded_paths(
    promotion_harness: PromotionHarness,
    excluded_paths: object,
) -> None:
    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        exclude_paths=excluded_paths,  # type: ignore[arg-type]
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "invalid_excluded_path"


def test_promote_rejects_line_separator_in_reviewed_excluded_path(
    promotion_harness: PromotionHarness,
) -> None:
    separator_path = "line\u2028break.txt"
    source = promotion_harness.github.prs[372]
    promotion_harness.github.prs[372] = replace(
        source,
        changed_paths=(*source.changed_paths, separator_path),
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        exclude_paths=(separator_path,),
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "invalid_excluded_path"


@pytest.mark.parametrize(
    "invalid_source",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(1.5, id="float"),
        pytest.param(None, id="none"),
        pytest.param(object(), id="object"),
        pytest.param((), id="empty"),
        pytest.param((372, 372), id="duplicate"),
        pytest.param((372, "373"), id="mixed"),
    ],
)
def test_promote_rejects_invalid_source_pr_values(
    promotion_harness: PromotionHarness,
    invalid_source: object,
) -> None:
    result = promotion_harness.service.promote(
        source_pr=invalid_source,  # type: ignore[arg-type]
        target_branch="main",
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "invalid_source_pr"


def test_promote_preserves_single_source_without_merge_commit(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.github.prs[372]
    promotion_harness.github.prs[372] = replace(
        source,
        merge_commit_sha=None,
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.state is LeaseState.PR_OPEN


def test_promote_rejects_gap_between_source_pull_requests(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_followup_source()
    promotion_harness.github.prs[source.number] = replace(
        source,
        base_sha="f" * 40,
    )

    result = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_sequence_gap"


def test_promote_rejects_source_base_outside_reviewed_head_ancestry(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.add_followup_source(
        change_feature=False,
        include_followup=False,
        team_text="team updated\n",
    )
    promotion_harness.make_target_conflict()
    source = promotion_harness.github.prs[372]
    promotion_harness.github.prs[372] = replace(
        source,
        base_sha=promotion_harness.git.resolve_ref("main"),
    )

    result = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        exclude_paths=("team.txt",),
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_base_not_ancestor"


def test_promote_rejects_merge_commit_outside_staging_history(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.add_followup_source(
        change_feature=False,
        include_followup=False,
        team_text="team updated\n",
    )
    promotion_harness.make_target_conflict()
    source = promotion_harness.github.prs[373]
    promotion_harness.github.prs[373] = replace(
        source,
        merge_commit_sha=promotion_harness.git.resolve_ref("main"),
    )

    result = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        exclude_paths=("team.txt",),
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_merge_not_in_staging"


def test_promote_prepares_worktree_before_production_verification(
    promotion_harness: PromotionHarness,
) -> None:
    prepared_path = promotion_harness.state_dir / "prepared.txt"
    prepare_script = (
        "from pathlib import Path; "
        f"Path({str(prepared_path)!r}).write_text('ready', encoding='utf-8')"
    )
    verify_script = (
        "from pathlib import Path; "
        f"assert Path({str(prepared_path)!r}).read_text(encoding='utf-8') == 'ready'"
    )
    promotion_harness.configure(
        prepare_command=(sys.executable, "-c", prepare_script),
        verify_production=((sys.executable, "-c", verify_script),),
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.state is LeaseState.PR_OPEN
    assert prepared_path.read_text(encoding="utf-8") == "ready"


def test_promote_rejects_dirty_prepare_output(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(
        prepare_command=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('prepared.txt').write_text('dirty')",
        ),
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_prepare_dirty"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize(
    "message",
    (
        "gh pr view failed (4): authentication required",
        "gh pr view failed (1): network unavailable",
    ),
)
def test_promote_classifies_github_source_view_failures_as_external(
    promotion_harness: PromotionHarness, message: str
) -> None:
    promotion_harness.github.error = ExternalServiceError(message)

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "source_pr_unavailable"
    assert not promotion_harness.registry.db_path.exists()


def test_promote_classifies_github_create_failure_as_external(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.github.create_error = ExternalServiceError(
        "gh pr create failed (1): network unavailable"
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "promotion_publish_failed"
    assert result.lease is not None
    current = promotion_harness.registry.get_lease(result.lease.id)
    assert current is not None
    assert current.state is LeaseState.ACTIVE
    assert current.worktree_path.exists()
    assert {
        event.event_type for event in promotion_harness.registry.list_events(current.id)
    } == {"promotion_publish_pending"}


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


def test_promote_default_policy_rejects_unapproved_self_merge(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372],
        review_decision="",
        author_login="steven",
        merged_by_login="steven",
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_not_approved"


@pytest.mark.parametrize("actor_login", ("steven", "dependabot[bot]"))
def test_promote_opt_in_policy_accepts_unapproved_self_merge(
    promotion_harness: PromotionHarness,
    actor_login: str,
) -> None:
    promotion_harness.configure(
        source_review_policy="approved_or_self_merged"
    )
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372],
        review_decision="",
        author_login=actor_login,
        merged_by_login=actor_login,
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert result.status == "ok"
    assert result.decision == "preview"


def test_promote_opt_in_policy_accepts_approved_source_without_identities(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(
        source_review_policy="approved_or_self_merged"
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert result.status == "ok"
    assert result.decision == "preview"


@pytest.mark.parametrize(
    ("source_change", "blocker"),
    (
        ({"state": "OPEN"}, "source_pr_not_merged"),
        ({"checks_passed": False}, "source_pr_checks_failed"),
        ({"base_ref": "main"}, "source_pr_base_mismatch"),
    ),
)
def test_promote_opt_in_policy_retains_source_gates(
    promotion_harness: PromotionHarness,
    source_change: dict[str, object],
    blocker: str,
) -> None:
    promotion_harness.configure(
        source_review_policy="approved_or_self_merged"
    )
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372],
        review_decision="",
        author_login="steven",
        merged_by_login="steven",
        **source_change,
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == blocker


@pytest.mark.parametrize(
    ("author_login", "merged_by_login"),
    (
        ("steven", "reviewer"),
        ("steven", None),
        (None, "steven"),
        (" ", " "),
        ("a--b", "a--b"),
        ("a" * 40, "a" * 40),
    ),
)
def test_promote_opt_in_policy_rejects_non_self_merged_source(
    promotion_harness: PromotionHarness,
    author_login: str | None,
    merged_by_login: str | None,
) -> None:
    promotion_harness.configure(
        source_review_policy="approved_or_self_merged"
    )
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372],
        review_decision="",
        author_login=author_login,
        merged_by_login=merged_by_login,
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_not_approved"


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


def test_promote_retries_blocked_verification_failure_with_exact_provenance(
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
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.lease is not None
    assert first.lease.state is LeaseState.BLOCKED

    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert len(promotion_harness.github.create_calls) == 1



def test_promote_rebuilds_content_mismatch_after_target_advances(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "promotion_content_mismatch"
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    target_sha = promotion_harness.advance_target_prerequisite()

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.id == first.lease.id
    assert second.lease.state is LeaseState.PR_OPEN
    assert second.lease.head_sha != blocked_head
    assert promotion_harness.git.commit_parents(second.lease.head_sha) == (target_sha,)
    assert len(promotion_harness.github.create_calls) == 1
    assert (second.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=new\n"

def test_promote_retries_blocked_transient_prepare_failure(
    promotion_harness: PromotionHarness,
) -> None:
    prepare_count = promotion_harness.state_dir / "blocked-prepare-count.txt"
    verify_count = promotion_harness.state_dir / "blocked-verify-count.txt"
    prepare_script = (
        "import sys; from pathlib import Path; "
        f"path = Path({str(prepare_count)!r}); "
        "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0; "
        "path.write_text(str(count + 1), encoding='utf-8'); "
        "sys.exit(7) if count == 0 else None"
    )
    verify_script = (
        "from pathlib import Path; "
        f"path = Path({str(verify_count)!r}); "
        "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0; "
        "path.write_text(str(count + 1), encoding='utf-8')"
    )
    promotion_harness.configure(
        prepare_command=(sys.executable, "-c", prepare_script),
        verify_production=((sys.executable, "-c", verify_script),),
    )

    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "promotion_prepare_failed"

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert prepare_count.read_text(encoding="utf-8") == "2"
    assert verify_count.read_text(encoding="utf-8") == "2"
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_retries_blocked_resume_verification_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify_count = promotion_harness.state_dir / "resume-verify-count.txt"
    verify_script = (
        "import sys; from pathlib import Path; "
        f"path = Path({str(verify_count)!r}); "
        "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0; "
        "path.write_text(str(count + 1), encoding='utf-8'); "
        "sys.exit(7) if count == 1 else None"
    )
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", verify_script),)
    )
    original_push = promotion_harness.git.push_branch

    def fail_push(*_: object) -> None:
        raise GitError("transient remote failure")

    monkeypatch.setattr(promotion_harness.git, "push_branch", fail_push)
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert first.status == "error"
    monkeypatch.setattr(promotion_harness.git, "push_branch", original_push)

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_verification_failed"

    third = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert third.decision == "ready"
    assert third.lease is not None
    assert third.lease.state is LeaseState.PR_OPEN
    assert verify_count.read_text(encoding="utf-8") == "4"
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_does_not_retry_blocked_verification_after_provenance_changes(
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
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.lease is not None
    git_command(
        first.lease.worktree_path,
        "commit",
        "--amend",
        "-q",
        "-m",
        "tampered promotion",
    )

    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert promotion_harness.github.create_calls == []



def test_promote_does_not_retry_blocked_verification_from_forged_target_parent(
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
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.lease is not None
    worktree_path = first.lease.worktree_path
    target_tree = git_command(worktree_path, "rev-parse", "HEAD^^{tree}")
    foreign_target = git_command(
        worktree_path,
        "commit-tree",
        target_tree,
        "-m",
        "foreign target",
    )
    promotion_tree = git_command(worktree_path, "rev-parse", "HEAD^{tree}")
    source = promotion_harness.github.prs[372]
    message = "\n".join(
        (
            "Promote PR #372 to main",
            "",
            "AWF-Source-PR: 372",
            f"AWF-Source-Base: {source.base_sha}",
            f"AWF-Source-Head: {source.head_sha}",
            f"AWF-Target-Base: {foreign_target}",
            f"AWF-Lease-ID: {first.lease.id}",
        )
    )
    forged_promotion = git_command(
        worktree_path,
        "commit-tree",
        promotion_tree,
        "-p",
        foreign_target,
        "-m",
        message,
    )
    git_command(worktree_path, "reset", "--hard", "-q", forged_promotion)
    blocked = promotion_harness.registry.get_lease(first.lease.id)
    assert blocked is not None
    promotion_harness.registry.transition(
        blocked.id,
        LeaseState.BLOCKED,
        expected_version=blocked.version,
        event_type="promotion_blocked",
        summary="promotion_apply_failed: production verification failed",
        head_sha=forged_promotion,
    )

    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert promotion_harness.github.create_calls == []


def test_promote_does_not_retry_blocked_verification_from_forged_source_base(
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
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.lease is not None
    worktree_path = first.lease.worktree_path
    source = promotion_harness.github.prs[372]
    target_sha = git_command(worktree_path, "rev-parse", "HEAD^")
    message = git_command(worktree_path, "log", "-1", "--format=%B").replace(
        f"AWF-Source-Base: {source.base_sha}",
        f"AWF-Source-Base: {target_sha}",
    )
    git_command(worktree_path, "commit", "--amend", "-q", "-m", message)
    forged_promotion = git_command(worktree_path, "rev-parse", "HEAD")
    blocked = promotion_harness.registry.get_lease(first.lease.id)
    assert blocked is not None
    promotion_harness.registry.transition(
        blocked.id,
        LeaseState.BLOCKED,
        expected_version=blocked.version,
        event_type="promotion_blocked",
        summary="promotion_apply_failed: production verification failed",
        head_sha=forged_promotion,
    )

    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert promotion_harness.github.create_calls == []


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



def test_promote_classifies_remote_fetch_failure_as_external(
    promotion_harness: PromotionHarness,
) -> None:
    origin = git_command(promotion_harness.repo, "remote", "get-url", "origin")
    git_command(
        promotion_harness.repo,
        "remote",
        "set-url",
        "origin",
        str(promotion_harness.repo.parent / "unreachable-origin.git"),
    )
    try:
        result = promotion_harness.service.promote(
            source_pr=372,
            target_branch="main",
            apply=True,
        )
    finally:
        git_command(promotion_harness.repo, "remote", "set-url", "origin", origin)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "source_delta_unavailable"
    assert promotion_harness.registry.list_leases() == []
    assert len(promotion_harness.git.list_worktrees()) == 1

def test_promote_retries_publish_and_prepares_when_marker_is_missing(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_count = promotion_harness.state_dir / "prepare-retry-count.txt"
    prepare_script = (
        "from pathlib import Path; "
        f"path = Path({str(prepare_count)!r}); "
        "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0; "
        "path.write_text(str(count + 1), encoding='utf-8')"
    )
    verify_count = promotion_harness.state_dir / "verify-retry-count.txt"
    verify_script = (
        "from pathlib import Path; "
        f"path = Path({str(verify_count)!r}); "
        "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0; "
        "path.write_text(str(count + 1), encoding='utf-8')"
    )
    promotion_harness.configure(
        prepare_command=(sys.executable, "-c", prepare_script),
        verify_production=((sys.executable, "-c", verify_script),),
    )
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
    assert first.lease is not None
    (
        promotion_harness.state_dir / "prepare" / f"{first.lease.id}.json"
    ).unlink()

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert first.status == "error"
    assert first.exit_code == 4
    assert first.lease.state is LeaseState.ACTIVE
    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert len(promotion_harness.github.create_calls) == 1
    assert prepare_count.read_text(encoding="utf-8") == "2"
    assert verify_count.read_text(encoding="utf-8") == "2"


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
    prepare_count = promotion_harness.state_dir / "prepare-count.txt"
    prepare_script = (
        "from pathlib import Path; "
        f"path = Path({str(prepare_count)!r}); "
        "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0; "
        "path.write_text(str(count + 1), encoding='utf-8')"
    )
    promotion_harness.configure(
        prepare_command=(sys.executable, "-c", prepare_script),
    )
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
    assert prepare_count.read_text(encoding="utf-8") == "2"


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
                "import sys; sys.stderr.write('https://user:' + 'secret' * 1000 + '@host ' + 'x' * 1_000_000); sys.exit(7)",
            ),
        )
    )

    with pytest.raises(RuntimeError) as error:
        promotion_harness.service._verify_promotion(promotion_harness.repo)

    detail = str(error.value)
    assert "secret" not in detail
    assert "<redacted>" in detail
    assert len(detail.encode("utf-8")) <= 600


def test_production_verifier_kills_timeout_descendant(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.worktrees import service as service_module

    pid_path = tmp_path / "descendant.pid"
    monkeypatch.setattr(service_module, "_PRODUCTION_VERIFY_TIMEOUT_SECONDS", 1.0)
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


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("dirty", "dirty_worktree"),
        ("untracked", "dirty_worktree"),
        ("closed", "pr_not_merged"),
        ("head_mismatch", "head_mismatch"),
        ("unmanaged", "unmanaged_lease"),
        ("retain", "retained_lease"),
        ("deployment_unknown", "deployment_not_healthy"),
        ("deployment_failed", "deployment_not_healthy"),
    ],
)
def test_finish_preserves_unsafe_worktree(
    promotion_harness: PromotionHarness, mutation: str, code: str
) -> None:
    lease = promotion_harness.merged_promotion(mutation)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert code in {item["code"] for item in result.blockers}
    assert lease.worktree_path.exists()


def test_finish_removes_healthy_managed_promotion(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert not lease.worktree_path.exists()
    removed = promotion_harness.registry.get_lease(lease.id)
    assert removed is not None
    assert removed.state is LeaseState.REMOVED


def test_finish_previews_without_mutating(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=False)

    assert result.decision == "preview"
    assert lease.worktree_path.exists()
    assert promotion_harness.registry.get_lease(lease.id).state is LeaseState.CLEANABLE


def test_finish_classifies_github_view_failure_as_external(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    promotion_harness.github.error = ExternalServiceError(
        "gh pr view failed (4): authentication required"
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "github_refresh_failed"
    assert lease.worktree_path.exists()
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.CLEANABLE
    assert current.deployment_state is DeploymentState.HEALTHY


@pytest.mark.parametrize(
    ("value", "expected_seconds"),
    [("15s", 15), ("2m", 120), ("3h", 10_800), ("7d", 604_800)],
)
def test_gc_parses_supported_age_thresholds(
    harness: Harness, value: str, expected_seconds: int
) -> None:
    assert harness.service._parse_age_threshold(value) == timedelta(
        seconds=expected_seconds
    )


def test_gc_is_preview_by_default_and_rechecks_each_candidate(
    harness: Harness,
) -> None:
    safe = harness.merged_feature(age_days=10)
    dirty = harness.merged_feature(age_days=10, initiative="dirty")
    (dirty.worktree_path / "local.txt").write_text("keep", encoding="utf-8")

    preview = harness.service.gc(merged=True, older_than="7d", apply=False)

    assert preview.decision == "preview"
    assert safe.worktree_path.exists()
    assert dirty.worktree_path.exists()

    applied = harness.service.gc(merged=True, older_than="7d", apply=True)

    assert applied.decision == "removed"
    assert not safe.worktree_path.exists()
    assert dirty.worktree_path.exists()


def test_finish_classifies_nonzero_deployment_probe_as_external(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    probes: list[list[str]] = []

    def regressed_probe(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        probes.append(command)
        return subprocess.CompletedProcess(command, 1, "", "deployment regressed")

    promotion_harness.service.deployment_runner = regressed_probe

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "deployment_probe_failed"
    assert probes == [[sys.executable, "-c", "pass"]]
    assert lease.worktree_path.exists()
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.CLEANABLE
    assert current.deployment_state is DeploymentState.HEALTHY


def test_finish_classifies_timed_out_deployment_probe_as_external(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")

    def failed_probe(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 30)

    promotion_harness.service.deployment_runner = failed_probe

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "deployment_probe_failed"
    assert lease.worktree_path.exists()
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.CLEANABLE
    assert current.deployment_state is DeploymentState.HEALTHY


def test_finish_classifies_malformed_deployment_probe_as_external(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")

    class MalformedCompleted:
        returncode = None

    promotion_harness.service.deployment_runner = lambda *_args, **_kwargs: (
        MalformedCompleted()
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "deployment_probe_failed"
    assert lease.worktree_path.exists()
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.CLEANABLE
    assert current.deployment_state is DeploymentState.HEALTHY


def test_finish_requires_a_fresh_healthy_deployment_probe(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    probes: list[list[str]] = []

    def healthy_probe(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        probes.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    promotion_harness.service.deployment_runner = healthy_probe

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert probes == [[sys.executable, "-c", "pass"]]
    assert not lease.worktree_path.exists()


def test_finish_preserves_promotion_when_forced_probe_cannot_be_recorded(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_transition = promotion_harness.registry.transition

    def transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "cleanup_deployment_probe":
            raise RuntimeError("database unavailable")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(promotion_harness.registry, "transition", transition)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert "deployment_not_healthy" in {item["code"] for item in result.blockers}
    assert "deployment_probe_record_failed" in {
        item["code"] for item in result.warnings
    }
    assert lease.worktree_path.exists()


def test_finish_rejects_a_post_merge_pr_head_advance(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    (lease.worktree_path / "advanced.txt").write_text("advanced\n", encoding="utf-8")
    git_command(lease.worktree_path, "add", "advanced.txt")
    git_command(lease.worktree_path, "commit", "-q", "-m", "advance merged PR")
    advanced_head = git_command(lease.worktree_path, "rev-parse", "HEAD")
    promotion_harness.github.prs[lease.target_pr] = replace(
        promotion_harness.github.prs[lease.target_pr], head_sha=advanced_head
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert "head_mismatch" in {item["code"] for item in result.blockers}
    assert lease.worktree_path.exists()


def test_gc_rejects_a_post_merge_feature_head_advance(harness: Harness) -> None:
    lease = harness.merged_feature(age_days=10)
    (lease.worktree_path / "advanced.txt").write_text("advanced\n", encoding="utf-8")
    git_command(lease.worktree_path, "add", "advanced.txt")
    git_command(lease.worktree_path, "commit", "-q", "-m", "advance merged PR")
    advanced_head = git_command(lease.worktree_path, "rev-parse", "HEAD")
    harness.github.prs[lease.target_pr] = replace(
        harness.github.prs[lease.target_pr], head_sha=advanced_head
    )

    result = harness.service.gc(merged=True, older_than="7d", apply=True)

    assert result.status == "blocked"
    assert "head_mismatch" in {item["code"] for item in result.blockers}
    assert lease.worktree_path.exists()


def test_finish_rejects_a_symlinked_lease_leaf_before_git_inspection(
    promotion_harness: PromotionHarness, tmp_path: Path
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    relocated = tmp_path / "outside-worktree"
    lease.worktree_path.rename(relocated)
    lease.worktree_path.symlink_to(relocated, target_is_directory=True)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=False)

    assert result.status == "blocked"
    assert "unsafe_worktree_path" in {item["code"] for item in result.blockers}
    assert lease.worktree_path.is_symlink()
    assert relocated.is_dir()


def test_finish_rejects_a_symlinked_lease_path_component_before_git_inspection(
    promotion_harness: PromotionHarness, tmp_path: Path
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    relocated_cache = tmp_path / "relocated-cache"
    promotion_harness.cache_dir.rename(relocated_cache)
    promotion_harness.cache_dir.symlink_to(relocated_cache, target_is_directory=True)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=False)

    assert result.status == "blocked"
    assert "unsafe_worktree_path" in {item["code"] for item in result.blockers}
    assert promotion_harness.cache_dir.is_symlink()
    assert lease.worktree_path.is_dir()


@pytest.mark.parametrize("substitution", ("leaf", "parent"))
def test_finish_blocks_adopted_imported_worktree_symlink_substitution(
    harness: Harness, tmp_path: Path, substitution: str
) -> None:
    lease = adopted_imported_release_worktree(harness, tmp_path / "import-root")
    assert lease.target_pr is not None
    expected_path = lease.worktree_path
    if substitution == "leaf":
        relocated = tmp_path / "relocated-legacy-release"
        expected_path.rename(relocated)
        expected_path.symlink_to(relocated, target_is_directory=True)
        assert expected_path.is_symlink()
    else:
        relocated = tmp_path / "relocated-import-root"
        expected_path.parent.rename(relocated)
        expected_path.parent.symlink_to(relocated, target_is_directory=True)
        assert expected_path.parent.is_symlink()

    before = harness.registry.get_lease(lease.id)
    assert before is not None
    before_events = harness.registry.list_events(lease.id)
    before_reservation = harness.registry.get_cleanup_reservation(lease.id)
    assert before_reservation is None

    for apply in (False, True):
        result = harness.service.finish(pr_number=lease.target_pr, apply=apply)

        assert result.status == "blocked"
        assert "unsafe_worktree_path" in {
            item["code"] for item in result.blockers
        }
        assert harness.registry.get_lease(lease.id) == before
        assert harness.registry.list_events(lease.id) == before_events
        assert harness.registry.get_cleanup_reservation(lease.id) == before_reservation

    assert any(
        worktree.path == expected_path for worktree in harness.git.list_worktrees()
    )
    assert harness.git.resolve_ref(lease.branch) == lease.head_sha
    assert (
        git_command(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{lease.branch}",
        )
        == lease.head_sha
    )


def test_finish_does_not_recover_adopted_import_through_a_symlink(
    harness: Harness, tmp_path: Path
) -> None:
    lease = adopted_imported_release_worktree(harness, tmp_path / "import-root")
    assert lease.target_pr is not None
    reservation = harness.registry.reserve_cleanup(
        lease.id,
        expected_version=lease.version,
        branch_sha=lease.head_sha,
    )
    reserved = harness.registry.get_lease(lease.id)
    assert reserved is not None
    reserved_events = harness.registry.list_events(lease.id)
    relocated = tmp_path / "relocated-legacy-release"
    lease.worktree_path.rename(relocated)
    lease.worktree_path.symlink_to(relocated, target_is_directory=True)

    result = harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert "unsafe_worktree_path" in {item["code"] for item in result.blockers}
    assert harness.registry.get_lease(lease.id) == reserved
    assert harness.registry.list_events(lease.id) == reserved_events
    assert harness.registry.get_cleanup_reservation(lease.id) == reservation
    assert lease.worktree_path.is_symlink()
    assert relocated.is_dir()
    assert harness.git.resolve_ref(lease.branch) == lease.head_sha
    assert (
        git_command(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{lease.branch}",
        )
        == lease.head_sha
    )


def test_finish_releases_reservation_when_removal_fails_after_refresh_race(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")

    def failing_remove(path: Path) -> None:
        current = promotion_harness.registry.get_lease(lease.id)
        assert current is not None
        refresh_warnings: list[dict[str, str]] = []
        promotion_harness.service._refresh_lease(current, refresh_warnings)
        assert "lease_refresh_failed" in {
            item["code"] for item in refresh_warnings
        }
        raise GitError("simulated worktree removal failure")

    monkeypatch.setattr(promotion_harness.git, "remove_worktree", failing_remove)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert lease.worktree_path.exists()
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None


def test_finish_recovers_a_reserved_lease_after_post_remove_registry_failure(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_complete = promotion_harness.registry.complete_cleanup

    def failing_complete(*_: object, **__: object) -> Lease:
        raise RuntimeError("final cleanup write failed")

    monkeypatch.setattr(
        promotion_harness.registry, "complete_cleanup", failing_complete
    )
    first = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert first.status == "blocked"
    assert not lease.worktree_path.exists()
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is not None

    monkeypatch.setattr(
        promotion_harness.registry, "complete_cleanup", original_complete
    )
    recovered = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert recovered.decision == "removed"
    assert promotion_harness.registry.get_lease(lease.id).state is LeaseState.REMOVED


def test_finish_blocks_a_recreated_local_branch_while_ref_lock_is_held(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    git_command(
        promotion_harness.repo, "checkout", "-q", "-b", "local-race-source", lease.branch
    )
    (promotion_harness.repo / "local-race.txt").write_text("race\n", encoding="utf-8")
    git_command(promotion_harness.repo, "add", "local-race.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "local race")
    recreated_head = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    original_remove = promotion_harness.git.remove_worktree
    original_delete = promotion_harness.git.delete_branch_if_at
    cas_calls: list[tuple[str, str]] = []
    race_codes: list[int] = []

    def remove_then_attempt_recreate(path: Path) -> None:
        original_remove(path)
        raced = subprocess.run(
            [
                "git",
                "update-ref",
                f"refs/heads/{lease.branch}",
                recreated_head,
                lease.head_sha,
            ],
            cwd=promotion_harness.repo,
            check=False,
            capture_output=True,
            text=True,
        )
        race_codes.append(raced.returncode)

    def record_cas(branch: str, expected_sha: str) -> None:
        cas_calls.append((branch, expected_sha))
        original_delete(branch, expected_sha)

    monkeypatch.setattr(
        promotion_harness.git, "remove_worktree", remove_then_attempt_recreate
    )
    monkeypatch.setattr(promotion_harness.git, "delete_branch_if_at", record_cas)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert cas_calls == [(lease.branch, lease.head_sha)]
    assert race_codes and race_codes[0] != 0
    assert lease.branch not in git_command(
        promotion_harness.repo, "branch", "--format=%(refname:short)"
    ).splitlines()


def test_finish_preserves_a_recreated_remote_branch_with_a_cas_delete(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    git_command(
        promotion_harness.repo, "checkout", "-q", "-b", "remote-race-source", lease.branch
    )
    (promotion_harness.repo / "remote-race.txt").write_text("race\n", encoding="utf-8")
    git_command(promotion_harness.repo, "add", "remote-race.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "remote race")
    recreated_head = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    original_remove = promotion_harness.git.remove_worktree

    def remove_then_recreate_remote(path: Path) -> None:
        original_remove(path)
        git_command(
            promotion_harness.repo,
            "push",
            "-q",
            "origin",
            f"{recreated_head}:refs/heads/{lease.branch}",
        )

    monkeypatch.setattr(
        promotion_harness.git, "remove_worktree", remove_then_recreate_remote
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    remote = git_command(
        promotion_harness.repo,
        "ls-remote",
        "--heads",
        "origin",
        lease.branch,
    )
    assert remote.split()[0] == recreated_head
    assert "remote_branch_cleanup_failed" in {item["code"] for item in result.warnings}
    assert promotion_harness.registry.list_events(lease.id)[-1].event_type == (
        "remote_branch_cleanup_failed"
    )


def test_finish_rejects_a_configured_symlink_cache_root(
    promotion_harness: PromotionHarness, tmp_path: Path
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    configured_cache = tmp_path / "configured-cache"
    configured_cache.symlink_to(promotion_harness.cache_dir, target_is_directory=True)
    service = WorktreeService(
        promotion_harness.registry,
        promotion_harness.git,
        config=promotion_harness.config,
        github=promotion_harness.github,
        cache_dir=configured_cache,
        state_dir=promotion_harness.state_dir,
        lock_dir=promotion_harness.lock_dir,
    )

    result = service.finish(pr_number=lease.target_pr, apply=False)

    assert result.status == "blocked"
    assert "unsafe_worktree_path" in {item["code"] for item in result.blockers}
    assert configured_cache.is_symlink()
    assert lease.worktree_path.exists()


def test_finish_holds_branch_ref_lock_against_post_reservation_ref_race(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    git_command(
        promotion_harness.repo, "checkout", "-q", "-b", "lock-race-source", lease.branch
    )
    (promotion_harness.repo / "lock-race.txt").write_text("race\n", encoding="utf-8")
    git_command(promotion_harness.repo, "add", "lock-race.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "lock race")
    advanced_head = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    git_command(
        promotion_harness.repo, "branch", "lock-race-alternate", lease.head_sha
    )
    original_lock = promotion_harness.git.hold_worktree_branch_if_at
    attempts: list[int] = []

    @contextmanager
    def lock_then_race(path: Path, branch: str, expected_sha: str):
        with original_lock(path, branch, expected_sha):
            ref_race = subprocess.run(
                [
                    "git",
                    "update-ref",
                    f"refs/heads/{branch}",
                    advanced_head,
                    expected_sha,
                ],
                cwd=promotion_harness.repo,
                check=False,
                capture_output=True,
                text=True,
            )
            detach_race = subprocess.run(
                ["git", "checkout", "--detach"],
                cwd=path,
                check=False,
                capture_output=True,
                text=True,
            )
            alternate_race = subprocess.run(
                ["git", "checkout", "-q", "lock-race-alternate"],
                cwd=path,
                check=False,
                capture_output=True,
                text=True,
            )
            attempts.extend(
                (
                    ref_race.returncode,
                    detach_race.returncode,
                    alternate_race.returncode,
                )
            )
            yield

    monkeypatch.setattr(
        promotion_harness.git, "hold_worktree_branch_if_at", lock_then_race
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert attempts and all(attempt != 0 for attempt in attempts)


def test_finish_releases_reservation_when_worktree_head_changes_before_hold(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_reserve = promotion_harness.registry.reserve_cleanup

    def reserve_then_detach(*args: object, **kwargs: object):
        reservation = original_reserve(*args, **kwargs)
        git_command(lease.worktree_path, "checkout", "--detach")
        return reservation

    monkeypatch.setattr(
        promotion_harness.registry, "reserve_cleanup", reserve_then_detach
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert "branch_head_mismatch" in {item["code"] for item in result.blockers}
    assert lease.worktree_path.exists()
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None


def test_finish_rechecks_mutation_before_remove_and_releases_reservation(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_reserve = promotion_harness.registry.reserve_cleanup
    original_lock = promotion_harness.git.hold_worktree_branch_if_at
    lock_calls: list[tuple[Path, str, str]] = []

    def reserve_then_dirty(*args: object, **kwargs: object):
        reservation = original_reserve(*args, **kwargs)
        (lease.worktree_path / "raced.txt").write_text("raced\n", encoding="utf-8")
        return reservation

    @contextmanager
    def record_lock(path: Path, branch: str, expected_sha: str):
        lock_calls.append((path, branch, expected_sha))
        with original_lock(path, branch, expected_sha):
            yield

    monkeypatch.setattr(
        promotion_harness.registry, "reserve_cleanup", reserve_then_dirty
    )
    monkeypatch.setattr(
        promotion_harness.git, "hold_worktree_branch_if_at", record_lock
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert "dirty_worktree" in {item["code"] for item in result.blockers}
    assert lock_calls == [(lease.worktree_path, lease.branch, lease.head_sha)]
    assert lease.worktree_path.exists()
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None


def test_gc_propagates_external_provider_error_after_completed_removal(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed_lease = harness.merged_feature(age_days=10, initiative="removed-first")
    failed_lease = harness.merged_feature(age_days=10, initiative="failed-second")
    with sqlite3.connect(harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", removed_lease.id),
        )
        connection.execute(
            "UPDATE worktree_leases SET created_at = ? WHERE id = ?",
            ("2001-01-01T00:00:00+00:00", failed_lease.id),
        )
    original_view = harness.github.view_pr

    def fail_second_view(number: int) -> PullRequest:
        if number == failed_lease.target_pr:
            raise ExternalServiceError("gh pr view failed: network unavailable")
        return original_view(number)

    monkeypatch.setattr(harness.github, "view_pr", fail_second_view)

    result = harness.service.gc(merged=True, older_than="7d", apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "github_refresh_failed"
    assert [lease.id for lease in result.leases] == [removed_lease.id]
    assert {
        action["kind"] for action in result.actions
    } >= {"remove_worktree"}
    assert not removed_lease.worktree_path.exists()
    assert failed_lease.worktree_path.exists()
    assert harness.registry.get_lease(removed_lease.id).state is LeaseState.REMOVED
    assert harness.registry.get_lease(failed_lease.id).state is LeaseState.CLEANABLE


def test_finish_releases_reservation_for_post_lock_github_failure(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_view = promotion_harness.github.view_pr
    view_calls = 0

    def fail_post_lock_view(number: int) -> PullRequest:
        nonlocal view_calls
        view_calls += 1
        if view_calls == 4:
            raise ExternalServiceError("gh pr view failed: network unavailable")
        return original_view(number)

    monkeypatch.setattr(promotion_harness.github, "view_pr", fail_post_lock_view)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "github_refresh_failed"
    assert view_calls == 4
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.CLEANABLE
    assert lease.worktree_path.exists()


def test_finish_propagates_remote_branch_delete_failure_after_removal(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")

    def remote_failure(*_args: object, **_kwargs: object) -> None:
        raise GitRemoteError(
            "git push failed (128): Permission denied (publickey). "
            "Could not read from remote repository."
        )

    monkeypatch.setattr(
        promotion_harness.git,
        "delete_remote_branch_if_at",
        remote_failure,
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "remote_branch_cleanup_failed"
    assert [warning["code"] for warning in result.warnings].count(
        "remote_branch_cleanup_failed"
    ) == 1
    assert result.lease is not None
    assert result.lease.id == lease.id
    assert result.lease.state is LeaseState.REMOVED
    assert any(
        action["kind"] == "remove_worktree" and action["lease_id"] == lease.id
        for action in result.actions
    )
    assert not lease.worktree_path.exists()
    assert promotion_harness.registry.get_lease(lease.id).state is LeaseState.REMOVED
    events = promotion_harness.registry.list_events(lease.id)
    assert [event.event_type for event in events].count("worktree_removed") == 1
    assert [event.event_type for event in events].count(
        "remote_branch_cleanup_failed"
    ) == 1
    assert events[-1].event_type == "remote_branch_cleanup_failed"


def test_finish_keeps_local_branch_delete_failure_as_warning(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")

    def local_failure(*_args: object, **_kwargs: object) -> None:
        raise GitError("local ref changed")

    monkeypatch.setattr(
        promotion_harness.git,
        "delete_branch_if_at",
        local_failure,
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert result.exit_code == 0
    assert "local_branch_cleanup_failed" in {
        warning["code"] for warning in result.warnings
    }
    assert not lease.worktree_path.exists()


def test_gc_propagates_nested_finish_external_error_after_completed_removal(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed_lease = harness.merged_feature(age_days=10, initiative="removed-first")
    failed_lease = harness.merged_feature(age_days=10, initiative="failed-second")
    with sqlite3.connect(harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", removed_lease.id),
        )
        connection.execute(
            "UPDATE worktree_leases SET created_at = ? WHERE id = ?",
            ("2001-01-01T00:00:00+00:00", failed_lease.id),
        )
    original_view = harness.github.view_pr
    failed_view_calls = 0

    def fail_nested_view(number: int) -> PullRequest:
        nonlocal failed_view_calls
        if number == failed_lease.target_pr:
            failed_view_calls += 1
            if failed_view_calls == 3:
                raise ExternalServiceError("gh pr view failed: network unavailable")
        return original_view(number)

    monkeypatch.setattr(harness.github, "view_pr", fail_nested_view)

    result = harness.service.gc(merged=True, older_than="7d", apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "github_refresh_failed"
    assert [lease.id for lease in result.leases] == [
        removed_lease.id,
        failed_lease.id,
    ]
    assert any(
        action["kind"] == "remove_worktree" and action["lease_id"] == removed_lease.id
        for action in result.actions
    )
    assert not removed_lease.worktree_path.exists()
    assert failed_lease.worktree_path.exists()