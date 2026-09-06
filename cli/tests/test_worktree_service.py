from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import awf.worktrees.service as service_module
from awf.worktrees.config import WorktreeConfig
from awf.worktrees.evidence import (
    DeploymentEvidenceResponse,
    EvidenceProbeResult,
)
from awf.worktrees.git import (
    GitClient,
    GitError,
    GitPatchConflict,
    GitRemoteError,
    GitWorktree,
)
from awf.worktrees.github import ExternalServiceError, PullRequest
from awf.worktrees.models import (
    DeploymentState,
    Lease,
    LeaseState,
    PromotionMode,
    Purpose,
    ResolutionState,
)
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
    merged_find_calls: list[tuple[str, str]] = field(default_factory=list)

    def view_pr(self, number: int) -> PullRequest:
        self.view_calls.append(number)
        if self.error is not None:
            raise self.error
        return self.prs[number]

    def find_open_pr(self, *, head: str, base: str) -> PullRequest | None:
        self.find_calls.append((head, base))
        return self.open_prs.get((head, base))

    def find_merged_prs(self, *, head: str, base: str) -> tuple[PullRequest, ...]:
        self.merged_find_calls.append((head, base))
        return tuple(
            pull for pull in self.prs.values()
            if pull.state == "MERGED" and pull.head_ref == head and pull.base_ref == base
        )

    def find_pr(self, *, head: str, base: str) -> PullRequest | None:
        self.find_calls.append((head, base))
        candidates = {
            pull_request.number: pull_request
            for pull_request in (*self.prs.values(), *self.open_prs.values())
        }
        return next(
            (
                pull_request
                for pull_request in candidates.values()
                if pull_request.head_ref == head and pull_request.base_ref == base
            ),
            None,
        )

    def find_open_prs_by_prefix(
        self, *, base: str, head_prefix: str
    ) -> tuple[PullRequest, ...]:
        return tuple(
            pull_request
            for pull_request in self.open_prs.values()
            if pull_request.state == "OPEN"
            and pull_request.base_ref == base
            and pull_request.head_ref.startswith(head_prefix)
        )

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
            base_sha=(
                GitClient(self.repository_root).resolve_ref(
                    f"refs/remotes/origin/{base}"
                )
                if self.repository_root is not None
                else "target-base"
            ),
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
            body=body,
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
    return pull_request(state="MERGED", merge_commit_sha="f" * 40, **kwargs)


def closed_pr(**kwargs: object) -> PullRequest:
    return pull_request(state="CLOSED", **kwargs)


class StubEvidenceExecutor:
    def __init__(
        self,
        status: str = "healthy",
        *,
        production_image_git_sha: str | None = None,
    ) -> None:
        self.status = status
        self.production_image_git_sha = production_image_git_sha
        self.failure_code: str | None = None
        self.requests: list[object] = []

    def execute(self, adapter: object, request: object) -> EvidenceProbeResult:
        self.requests.append(request)
        received_at = datetime.now(timezone.utc)
        if self.failure_code is not None:
            return EvidenceProbeResult(None, received_at, self.failure_code)
        return EvidenceProbeResult(
            DeploymentEvidenceResponse(
                protocol=request.protocol,
                request_id=request.request_id,
                repository_id=request.repository_id,
                subject_revision=request.subject_revision,
                status=self.status,
                observed_at=datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                production_image_git_sha=self.production_image_git_sha,
            ),
            received_at,
        )


def _operator_home(tmp_path: Path, repository_id: str) -> Path:
    home_dir = tmp_path / "operator-home"
    config_path = home_dir / ".config" / "awf" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    executable = config_path.parent / "adapters" / "deployment-adapter"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    config_path.write_text(
        "[worktree.deployment.adapters."
        + json.dumps(repository_id)
        + "]\ncommand = ["
        + json.dumps(str(executable))
        + "]\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return home_dir


@dataclass
class Harness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    state_dir: Path
    lock_dir: Path
    github: FakeGitHub
    home_dir: Path
    evidence_executor: StubEvidenceExecutor
    service: WorktreeService

    @classmethod
    def create(cls, tmp_path: Path) -> Harness:
        repo = make_repository(tmp_path)
        git = GitClient(repo)
        registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
        cache_dir = tmp_path / "cache"
        state_dir = tmp_path / "state"
        lock_dir = tmp_path / "locks"
        github = FakeGitHub(repository_root=repo)
        home_dir = _operator_home(tmp_path, git.repository_id())
        evidence_executor = StubEvidenceExecutor()
        return cls(
            repo=repo,
            git=git,
            registry=registry,
            cache_dir=cache_dir,
            state_dir=state_dir,
            lock_dir=lock_dir,
            home_dir=home_dir,
            evidence_executor=evidence_executor,
            github=github,
            service=WorktreeService(
                registry,
                git,
                config=WorktreeConfig(default_base="staging"),
                github=github,
                evidence_executor=evidence_executor,
                cache_dir=cache_dir,
                state_dir=state_dir,
                lock_dir=lock_dir,
                home_dir=home_dir,
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
            github=self.github,
            evidence_executor=self.evidence_executor,
            cache_dir=self.cache_dir,
            state_dir=self.state_dir,
            lock_dir=self.lock_dir,
            home_dir=self.home_dir,
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
    home_dir: Path
    evidence_executor: StubEvidenceExecutor
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
        home_dir = _operator_home(tmp_path, git.repository_id())
        evidence_executor = StubEvidenceExecutor()
        return cls(
            repo=repo,
            git=git,
            registry=registry,
            cache_dir=cache_dir,
            state_dir=state_dir,
            lock_dir=lock_dir,
            github=github,
            config=config,
            home_dir=home_dir,
            evidence_executor=evidence_executor,
            service=WorktreeService(
                registry,
                git,
                config=config,
                github=github,
                evidence_executor=evidence_executor,
                cache_dir=cache_dir,
                state_dir=state_dir,
                lock_dir=lock_dir,
                home_dir=home_dir,
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
            evidence_executor=self.evidence_executor,
            cache_dir=self.cache_dir,
            state_dir=self.state_dir,
            lock_dir=self.lock_dir,
            home_dir=self.home_dir,
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

    def add_divergent_same_file_non_overlapping_source(
        self, number: int = 373
    ) -> PullRequest:
        source_base_text = (
            "copy=source\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n"
        )
        source_head_text = source_base_text.replace("rank=old", "rank=new")
        target_text = source_base_text.replace("copy=source", "copy=target")
        branch = f"feature/pr-{number}"

        git_command(self.repo, "checkout", "-q", "staging")
        (self.repo / "feature.txt").write_text(source_base_text, encoding="utf-8")
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "source prerequisite")
        git_command(self.repo, "push", "-q", "origin", "staging")
        git_command(self.repo, "checkout", "-q", "-b", branch)
        base_sha = git_command(self.repo, "rev-parse", "HEAD")
        (self.repo / "feature.txt").write_text(source_head_text, encoding="utf-8")
        git_command(self.repo, "add", "feature.txt")
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
        git_command(self.repo, "checkout", "-q", "main")
        (self.repo / "feature.txt").write_text(target_text, encoding="utf-8")
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "production divergent change")
        git_command(self.repo, "push", "-q", "origin", "main")
        git_command(self.repo, "checkout", "-q", "staging")

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
            changed_paths=("feature.txt",),
            url=f"https://github.example/acme/repo/pull/{number}",
        )
        self.github.prs[number] = source
        return source

    def add_renamed_source(self, number: int = 373) -> PullRequest:
        branch = f"feature/pr-{number}"
        git_command(self.repo, "checkout", "-q", "staging")
        base_sha = git_command(self.repo, "rev-parse", "HEAD")
        git_command(self.repo, "checkout", "-q", "-b", branch)
        git_command(self.repo, "mv", "feature.txt", "renamed-feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "rename source feature")
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
            "merge renamed source",
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
            changed_paths=("renamed-feature.txt",),
            url=f"https://github.example/acme/repo/pull/{number}",
        )
        self.github.prs[number] = source
        return source

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
        self.evidence_executor.failure_code = None
        self.evidence_executor.status = (
            "unknown"
            if mutation == "deployment_unknown"
            else "failed"
            if mutation == "deployment_failed"
            else "pending"
            if mutation == "deployment_pending"
            else "healthy"
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
                    "body": "AWF-No-Promote: true",
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
                    "mergeCommit,reviewDecision,statusCheckRollup,files,url,author,"
                    "mergedBy,body"
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
    assert pr.body == "AWF-No-Promote: true"


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


def test_github_client_fails_closed_when_open_pr_scan_reaches_limit(
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
            json.dumps([{}] * 1000),
            "",
        )

    with pytest.raises(ExternalServiceError, match="fail-closed"):
        GhClient(harness.repo, command_runner=runner).find_open_prs_by_prefix(
            base="staging",
            head_prefix="awf/sync-",
        )

    assert calls[0][-2:] == ["--limit", "1000"]


def test_github_client_prefix_scan_ignores_unrelated_malformed_body_and_keeps_unicode(
    harness: Harness,
) -> None:
    from awf.worktrees.github import GhClient

    body = "한" * 65536
    payload = [
        {
            "baseRefName": "staging",
            "headRefName": "user/unrelated",
            "body": {"malformed": True},
        },
        {
            "number": 42,
            "state": "OPEN",
            "baseRefName": "staging",
            "baseRefOid": "base-sha",
            "headRefName": "awf/sync-0123456789abcdef-0123456789ab/feature",
            "headRefOid": "head-sha",
            "mergeCommit": None,
            "reviewDecision": "",
            "statusCheckRollup": [],
            "files": [],
            "url": "https://github.example/acme/repo/pull/42",
            "author": {"login": "source-author"},
            "mergedBy": None,
            "body": body,
        },
    ]

    def runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    matches = GhClient(
        harness.repo, command_runner=runner
    ).find_open_prs_by_prefix(base="staging", head_prefix="awf/sync-")

    assert len(matches) == 1
    assert matches[0].body == body


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


    first = harness.service.status(initiative="reward-widget", refresh=True)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.CLEANABLE
    assert first.leases[0].deployment_state is DeploymentState.HEALTHY
    assert second.leases[0].version > first.leases[0].version
    assert len(harness.evidence_executor.requests) == 2


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
    assert harness.evidence_executor.requests == []

    result = harness.service.status(initiative="reward-widget", refresh=True)

    current = result.leases[0]
    assert current.state is LeaseState.BLOCKED
    assert current.deployment_state is DeploymentState.UNKNOWN
    assert current.head_sha == lease.head_sha
    assert harness.evidence_executor.requests == []
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
    assert harness.evidence_executor.requests == []

    first = harness.service.status(initiative="reward-widget", refresh=True)
    events = harness.registry.list_events(lease.id)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.BLOCKED
    assert first.leases[0].deployment_state is DeploymentState.UNKNOWN
    assert first.leases[0].version == unrelated_block.version + 1
    assert events[-1].event_type == "github_refresh_head_mismatch"
    assert events[-1].observed_head_sha == pull_request_head
    assert harness.evidence_executor.requests == []
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

    harness.evidence_executor.status = "failed"

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is LeaseState.BLOCKED
    assert result.leases[0].deployment_state is DeploymentState.FAILED


def test_refresh_keeps_promoted_lease_deploying_when_evidence_is_unknown(
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
    harness.evidence_executor.status = "unknown"

    first = harness.service.status(initiative="reward-widget", refresh=True)
    second = harness.service.status(initiative="reward-widget", refresh=True)

    assert first.leases[0].state is LeaseState.DEPLOYING
    assert first.leases[0].deployment_state is DeploymentState.UNKNOWN
    assert second.leases[0].version > first.leases[0].version

def test_refresh_rejects_missing_merge_sha_without_invoking_adapter(
    harness: Harness,
) -> None:
    acquired = harness.service.acquire(
        initiative="missing-merge",
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = pull_request(
        number=42,
        state="MERGED",
        head_sha=lease.head_sha,
        merge_commit_sha=None,
    )

    result = harness.service.status(initiative="missing-merge", refresh=True)

    assert result.leases[0].deployment_state is DeploymentState.UNKNOWN
    assert result.warnings[0]["code"] == "deployment_subject_revision_invalid"
    assert harness.evidence_executor.requests == []


def test_refresh_rejects_evidence_when_pr_merge_identity_changes_during_probe(
    harness: Harness,
) -> None:
    acquired = harness.service.acquire(
        initiative="merge-race",
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )
    assert acquired.lease is not None
    lease = harness.attach_pr(acquired.lease, 42)
    harness.github.prs[42] = merged_pr(number=42, head_sha=lease.head_sha)

    class MergeChangingExecutor(StubEvidenceExecutor):
        def execute(self, adapter: object, request: object) -> EvidenceProbeResult:
            result = super().execute(adapter, request)
            harness.github.prs[42] = replace(
                harness.github.prs[42], merge_commit_sha="d" * 40
            )
            return result

    executor = MergeChangingExecutor()
    harness.service.evidence_executor = executor

    result = harness.service.status(initiative="merge-race", refresh=True)

    assert result.leases[0].deployment_state is DeploymentState.UNKNOWN
    assert result.warnings[0]["code"] == "deployment_subject_changed"
    assert len(executor.requests) == 1


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


def test_refresh_fails_closed_when_evidence_executor_times_out(
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
    harness.evidence_executor.failure_code = "deployment_adapter_timeout"

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is LeaseState.DEPLOYING
    assert result.leases[0].deployment_state is DeploymentState.UNKNOWN
    assert result.warnings[0]["code"] == "deployment_adapter_timeout"


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


@pytest.mark.parametrize(
    ("source_pr", "exclude_paths"),
    [
        (372, ("feature.txt",)),
        (372, ("feature.txt", "feature.txt")),
        (372, ("/absolute.txt",)),
        (372, "feature.txt"),
    ],
)
def test_promote_rejects_invalid_out_of_order_combinations(
    promotion_harness: PromotionHarness,
    source_pr: int | tuple[int, ...],
    exclude_paths: object,
) -> None:
    result = promotion_harness.service.promote(
        source_pr=source_pr,
        exclude_paths=exclude_paths,  # type: ignore[arg-type]
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert result.blockers[0]["code"] == "invalid_out_of_order_promotion"
    assert promotion_harness.github.view_calls == []
    assert len(promotion_harness.git.list_worktrees()) == 1
    assert not promotion_harness.registry.db_path.exists()


def test_promotion_initiative_separates_out_of_order_identity() -> None:
    exact = WorktreeService._promotion_initiative(
        (372,),
        "main",
        promotion_mode=PromotionMode.EXACT,
    )
    out_of_order = WorktreeService._promotion_initiative(
        (372,),
        "main",
        promotion_mode=PromotionMode.OUT_OF_ORDER,
    )

    assert exact == "pr-372-to-main"
    assert out_of_order == "pr-372-to-main-out-of-order"


def test_promote_out_of_order_preview_reports_promotion_mode(
    promotion_harness: PromotionHarness,
) -> None:
    before_prs = dict(promotion_harness.github.prs)
    before_open_prs = dict(promotion_harness.github.open_prs)
    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert result.actions[0]["promotion_mode"] == "out_of_order"
    assert result.actions[0]["reviewed_paths"] == ["feature.txt"]
    assert len(promotion_harness.git.list_worktrees()) == 1
    assert not promotion_harness.registry.db_path.exists()
    assert not promotion_harness.cache_dir.exists()
    assert promotion_harness.github.create_calls == []
    assert promotion_harness.github.prs == before_prs
    assert promotion_harness.github.open_prs == before_open_prs


def test_promote_records_out_of_order_lease_provenance(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.github.prs[372]
    target_sha = promotion_harness.git.resolve_ref("origin/main")

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.lease is not None
    persisted = promotion_harness.registry.get_lease(result.lease.id)
    assert persisted is not None
    assert persisted.promotion_mode is PromotionMode.OUT_OF_ORDER
    assert persisted.source_base_sha == source.base_sha
    assert persisted.source_head_sha == source.head_sha
    assert persisted.target_base_sha == target_sha
    assert persisted.reviewed_paths == source.changed_paths


def test_promote_persists_ordered_out_of_order_source_pins(
    promotion_harness: PromotionHarness,
) -> None:
    second = promotion_harness.add_followup_source()

    result = promotion_harness.service.promote(
        source_pr=(372, second.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert [
        source.to_dict()
        for source in promotion_harness.registry.get_promotion_sources(result.lease.id)
    ] == [
        {
            "ordinal": ordinal,
            "source_pr": source.number,
            "base_ref": source.base_ref,
            "base_sha": source.base_sha,
            "head_sha": source.head_sha,
            "merge_sha": source.merge_commit_sha,
            "changed_paths": list(source.changed_paths),
        }
        for ordinal, source in enumerate(
            (promotion_harness.github.prs[372], second)
        )
    ]
    assert (result.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "feature updated\n"
    assert (result.lease.worktree_path / "followup.txt").read_text(
        encoding="utf-8"
    ) == "followup\n"


def test_promote_out_of_order_preview_uses_first_source_scalar_provenance(
    promotion_harness: PromotionHarness,
) -> None:
    first = promotion_harness.github.prs[372]
    second = promotion_harness.add_followup_source()

    result = promotion_harness.service.promote(
        source_pr=(first.number, second.number),
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert result.decision == "preview"
    action = result.actions[0]
    assert action["source_pr"] == first.number
    assert action["source_base_sha"] == first.base_sha
    assert action["source_head_sha"] == first.head_sha
    assert action["sources"][1]["head_sha"] == second.head_sha


def test_promote_out_of_order_rejects_reversed_source_sequence(
    promotion_harness: PromotionHarness,
) -> None:
    second = promotion_harness.add_followup_source()

    result = promotion_harness.service.promote(
        source_pr=(second.number, 372),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_sequence_order"


def test_promote_out_of_order_requires_merge_for_every_source(
    promotion_harness: PromotionHarness,
) -> None:
    second = promotion_harness.add_followup_source()
    promotion_harness.github.prs[second.number] = replace(
        second, merge_commit_sha=None
    )

    result = promotion_harness.service.promote(
        source_pr=(372, second.number),
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_invalid_oid"


def test_promote_persists_out_of_order_provenance_before_patch_application(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = promotion_harness.github.prs[372]
    target_sha = promotion_harness.git.resolve_ref("origin/main")
    leases_seen_during_patch: list[Lease] = []

    def fail_patch(_worktree: Path, _patch: bytes) -> None:
        leases = promotion_harness.registry.list_leases()
        assert len(leases) == 1
        persisted = promotion_harness.registry.get_lease(leases[0].id)
        assert persisted is not None
        leases_seen_during_patch.append(persisted)
        raise GitError("simulated patch failure")

    monkeypatch.setattr(
        promotion_harness.git,
        "apply_indexed_patch",
        fail_patch,
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_apply_failed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    persisted = promotion_harness.registry.get_lease(result.lease.id)
    assert persisted is not None
    assert persisted.state is LeaseState.BLOCKED
    assert [lease.id for lease in leases_seen_during_patch] == [result.lease.id]
    for lease in (*leases_seen_during_patch, persisted):
        assert lease.promotion_mode is PromotionMode.OUT_OF_ORDER
        assert lease.source_base_sha == source.base_sha
        assert lease.source_head_sha == source.head_sha
        assert lease.target_base_sha == target_sha
        assert lease.reviewed_paths == source.changed_paths


def test_promote_exact_blocks_divergent_same_file_followup(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_divergent_same_file_non_overlapping_source()

    result = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "staging_missing_main_delta"
    assert promotion_harness.github.create_calls == []




def test_promote_out_of_order_reuse_blocks_middle_source_pin_drift(
    promotion_harness: PromotionHarness,
) -> None:
    second = promotion_harness.add_followup_source()
    third = promotion_harness.add_followup_source(
        374,
        feature_text="feature final\n",
        include_followup=False,
    )
    first = promotion_harness.service.promote(
        source_pr=(372, second.number, third.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.decision == "ready"
    promotion_harness.github.prs[second.number] = replace(
        second,
        changed_paths=("followup.txt",),
    )
    reused = promotion_harness.service.promote(
        source_pr=(372, second.number, third.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert reused.status == "blocked"
    assert reused.blockers[0]["code"] == "promotion_provenance_changed"


def test_promote_out_of_order_recovers_conflict_with_all_source_pins(
    promotion_harness: PromotionHarness,
) -> None:
    second = promotion_harness.add_followup_source(
        change_feature=False,
        include_followup=True,
    )
    third = promotion_harness.add_followup_source(
        374,
        feature_text="feature final\n",
        include_followup=False,
    )
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "followup.txt").write_text(
        "target followup\n",
        encoding="utf-8",
    )
    git_command(promotion_harness.repo, "add", "followup.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "target followup")
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")

    blocked = promotion_harness.service.promote(
        source_pr=(372, second.number, third.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert blocked.status == "blocked"
    assert blocked.blockers[0]["code"] == "out_of_order_conflict"
    assert blocked.lease is not None
    assert blocked.lease.conflict_source_ordinal == 1
    assert blocked.lease.source_pr == 372
    assert blocked.lease.source_base_sha == (
        promotion_harness.github.prs[372].base_sha
    )
    assert blocked.lease.source_head_sha == (
        promotion_harness.github.prs[372].head_sha
    )
    assert [
        source.source_pr
        for source in promotion_harness.registry.get_promotion_sources(
            blocked.lease.id
        )
    ] == [372, second.number, third.number]
    (blocked.lease.worktree_path / "followup.txt").write_text(
        "resolved followup\n",
        encoding="utf-8",
    )

    resumed = promotion_harness.service.promote(
        source_pr=(372, second.number, third.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert (resumed.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "feature final\n"
    message = promotion_harness.git.commit_message(resumed.lease.worktree_path)
    for source in (promotion_harness.github.prs[372], second, third):
        assert f"AWF-Source-PR: {source.number}" in message
        assert f"AWF-Source-Merge: {source.merge_commit_sha}" in message


def test_promote_out_of_order_recovers_consecutive_source_conflicts(
    promotion_harness: PromotionHarness,
) -> None:
    second = promotion_harness.add_followup_source(
        change_feature=False,
        include_followup=True,
    )
    third = promotion_harness.add_followup_source(
        374,
        change_feature=False,
        include_followup=False,
        team_text="staging team\n",
    )
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "followup.txt").write_text(
        "target followup\n", encoding="utf-8"
    )
    (promotion_harness.repo / "team.txt").write_text(
        "target team\n", encoding="utf-8"
    )
    git_command(promotion_harness.repo, "add", "followup.txt", "team.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "target conflicts")
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")

    first_conflict = promotion_harness.service.promote(
        source_pr=(372, second.number, third.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first_conflict.lease is not None
    assert first_conflict.lease.conflict_source_ordinal == 1
    (first_conflict.lease.worktree_path / "followup.txt").write_text(
        "resolved followup\n", encoding="utf-8"
    )
    second_conflict = promotion_harness.service.promote(
        source_pr=(372, second.number, third.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert second_conflict.status == "blocked"
    assert second_conflict.blockers[0]["code"] == "out_of_order_conflict"
    assert second_conflict.lease is not None
    assert second_conflict.lease.conflict_source_ordinal == 2
    assert second_conflict.lease.conflicted_paths == ("team.txt",)
    assert promotion_harness.github.create_calls == []
    (second_conflict.lease.worktree_path / "team.txt").write_text(
        "resolved team\n", encoding="utf-8"
    )
    promoted = promotion_harness.service.promote(
        source_pr=(372, second.number, third.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert promoted.decision == "ready"
    assert promoted.lease is not None
    assert promoted.lease.conflict_source_ordinal is None
    assert (promoted.lease.worktree_path / "followup.txt").read_text(
        encoding="utf-8"
    ) == "resolved followup\n"
    assert (promoted.lease.worktree_path / "team.txt").read_text(
        encoding="utf-8"
    ) == "resolved team\n"
    message = promotion_harness.git.commit_message(promoted.lease.worktree_path)
    assert message.count("AWF-Source-PR:") == 3


def test_promote_lazily_backfills_verified_legacy_single_source_pins(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_divergent_same_file_non_overlapping_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.decision == "ready"
    assert first.lease is not None
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            "DELETE FROM promotion_lease_sources WHERE lease_id = ?",
            (first.lease.id,),
        )
    preview = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert preview.decision == "preview"
    assert promotion_harness.registry.get_promotion_sources_read_only(
        first.lease.id
    ) == ()
    reused = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert reused.decision == "reuse"
    assert promotion_harness.registry.get_promotion_sources(
        first.lease.id
    ) == promotion_harness.service._promotion_source_pins(
        first.lease, (source,)
    )


def test_promote_out_of_order_synthesizes_divergent_same_file_followup(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_divergent_same_file_non_overlapping_source()
    promotion_harness.configure(
        verify_production=(
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; assert Path('feature.txt').read_text()"
                    " == 'copy=target\\nstable-1\\nstable-2\\nstable-3\\nstable-4\\n"
                    "stable-5\\nrank=new\\n'"
                ),
            ),
        )
    )

    result = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.state is LeaseState.PR_OPEN
    assert result.lease.resolution_state is ResolutionState.AUTOMATIC
    assert result.lease.target_pr == 900
    assert (result.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == (
        "copy=target\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=new\n"
    )
    assert len(promotion_harness.github.create_calls) == 1
    expected_body = "\n".join(
        (
            "AWF-Source-PR: 373",
            f"AWF-Source-Base: {source.base_sha}",
            f"AWF-Source-Head: {source.head_sha}",
            f"AWF-Source-Merge: {source.merge_commit_sha}",
            f"AWF-Target-Base: {result.lease.target_base_sha}",
            f"AWF-Lease-ID: {result.lease.id}",
            "AWF-Promotion-Mode: out-of-order",
            "AWF-Resolution: automatic",
        )
    )
    assert promotion_harness.github.create_calls[0]["body"] == expected_body
    assert promotion_harness.git.commit_message(result.lease.worktree_path) == "\n\n".join(
        ("Promote PR #373 to main", expected_body)
    )
    assert result.actions == (
        {
            "kind": "verify_production",
            "argv": list(promotion_harness.config.verify_production[0]),
            "exit_code": 0,
            "stderr": "",
        },
    )

    reused = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert reused.decision == "reuse"
    assert len(promotion_harness.github.create_calls) == 1

def test_promote_out_of_order_reuse_requires_automatic_trailer(
    promotion_harness: PromotionHarness,
) -> None:
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.lease is not None
    message = promotion_harness.git.commit_message(first.lease.worktree_path)
    git_command(
        first.lease.worktree_path,
        "commit",
        "--amend",
        "-q",
        "-m",
        message.replace("AWF-Resolution: automatic", "AWF-Resolution: manual_reviewed"),
    )

    reused = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert reused.status == "blocked"
    assert reused.blockers[0]["code"] == "promotion_incomplete"
    assert len(promotion_harness.github.create_calls) == 1

def test_promote_out_of_order_blocks_renames_before_worktree_creation(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_renamed_source()

    result = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "unsupported_out_of_order_rename"
    assert promotion_harness.github.create_calls == []
    assert len(promotion_harness.git.list_worktrees()) == 1

@pytest.mark.parametrize(
    "state",
    (LeaseState.ACTIVE, LeaseState.BLOCKED, LeaseState.PR_OPEN),
    ids=("active", "blocked", "pr-open"),
)
def test_promote_out_of_order_reuse_blocks_legacy_rename_leases(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    state: LeaseState,
) -> None:
    source = promotion_harness.add_renamed_source()
    verify_count = promotion_harness.state_dir / "rename-verify-count.txt"
    promotion_harness.configure(
        verify_production=(
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(verify_count)!r}).touch()"
                ),
            ),
        )
    )
    target_ref = "origin/main"
    target_sha = promotion_harness.git.fetch_ref("main")
    lease = promotion_harness.service._new_promotion_lease(
        (source,),
        target_ref,
        target_sha,
        (),
        promotion_mode=PromotionMode.OUT_OF_ORDER,
    )
    promotion_harness.git.add_worktree(
        lease.worktree_path,
        lease.branch,
        target_sha,
    )
    lease = replace(
        lease,
        head_sha=promotion_harness.git.head_sha(lease.worktree_path),
    )
    lease = promotion_harness.registry.create_promotion_lease(
        lease,
        promotion_harness.service._promotion_source_pins(lease, (source,)),
    )
    if state is not LeaseState.ACTIVE:
        lease = promotion_harness.registry.transition(
            lease.id,
            state,
            expected_version=lease.version,
            event_type=(
                "promotion_blocked"
                if state is LeaseState.BLOCKED
                else "promotion_pr_open"
            ),
            pr_number=900 if state is LeaseState.PR_OPEN else None,
        )
    push_calls: list[str] = []

    def push_branch(_worktree: Path, branch: str) -> None:
        push_calls.append(branch)

    monkeypatch.setattr(promotion_harness.git, "push_branch", push_branch)
    result = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "unsupported_out_of_order_rename"
    assert not verify_count.exists()
    assert push_calls == []
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_publishes_reviewed_deletion(
    promotion_harness: PromotionHarness,
) -> None:
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "feature.txt").write_text(
        "feature\n", encoding="utf-8"
    )
    git_command(promotion_harness.repo, "add", "feature.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "production feature")
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    source = promotion_harness.add_followup_source(
        feature_text=None,
        include_followup=False,
    )

    result = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.state is LeaseState.PR_OPEN
    assert result.lease.resolution_state is ResolutionState.AUTOMATIC
    assert not (result.lease.worktree_path / "feature.txt").exists()
    assert len(promotion_harness.github.create_calls) == 1


@pytest.mark.parametrize(
    "synthetic_paths",
    ((), ("feature.txt", "unreviewed.txt")),
    ids=("empty-net-delta", "unreviewed-path"),
)
def test_promote_out_of_order_blocks_invalid_synthetic_paths(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_paths: tuple[str, ...],
) -> None:
    original_changed_paths = promotion_harness.git.changed_paths
    push_calls: list[str] = []

    def changed_paths(
        cwd: Path,
        base: str,
        head: str = "HEAD",
        *,
        find_renames: bool = False,
    ) -> tuple[str, ...]:
        if cwd != promotion_harness.repo and find_renames:
            return synthetic_paths
        return original_changed_paths(cwd, base, head, find_renames=find_renames)

    def push_branch(_worktree: Path, branch: str) -> None:
        push_calls.append(branch)

    monkeypatch.setattr(promotion_harness.git, "changed_paths", changed_paths)
    monkeypatch.setattr(promotion_harness.git, "push_branch", push_branch)

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_delta_mismatch"
    assert push_calls == []
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_recovers_pending_transition_and_reverifies(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify_count = promotion_harness.state_dir / "out-of-order-verify-count.txt"
    verify_script = (
        "from pathlib import Path; "
        f"path = Path({str(verify_count)!r}); "
        "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0; "
        "path.write_text(str(count + 1), encoding='utf-8')"
    )
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", verify_script),)
    )
    original_transition = promotion_harness.registry.transition

    def fail_pending_transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "promotion_publish_pending":
            raise RuntimeError("registry temporarily unavailable")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        promotion_harness.registry, "transition", fail_pending_transition
    )
    def unexpected_marker_check(*_args: object) -> bool:
        raise AssertionError("automatic promotion must not inspect manual markers")

    monkeypatch.setattr(
        promotion_harness.git,
        "staged_diff_has_conflict_markers",
        unexpected_marker_check,
    )
    monkeypatch.setattr(
        promotion_harness.git,
        "committed_diff_has_conflict_markers",
        unexpected_marker_check,
    )
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    monkeypatch.setattr(promotion_harness.registry, "transition", original_transition)

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.status == "blocked"
    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert second.lease.resolution_state is ResolutionState.AUTOMATIC
    assert second.lease.head_sha == promotion_harness.git.head_sha(
        second.lease.worktree_path
    )
    assert promotion_harness.github.prs[900].head_sha == second.lease.head_sha
    assert len(promotion_harness.github.create_calls) == 1
    assert verify_count.read_text(encoding="utf-8") == "3"


def test_promote_applies_only_source_pr_delta(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.github.prs[372]
    target_sha = promotion_harness.git.resolve_ref("origin/main")
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
    expected_body = "\n".join(
        (
            "AWF-Source-PR: 372",
            f"AWF-Source-Base: {source.base_sha}",
            f"AWF-Source-Head: {source.head_sha}",
            f"AWF-Target-Base: {target_sha}",
            f"AWF-Lease-ID: {result.lease.id}",
        )
    )
    assert promotion_harness.github.create_calls[0]["base"] == "main"
    assert promotion_harness.github.create_calls[0]["body"] == expected_body
    assert promotion_harness.git.commit_message(worktree) == "\n\n".join(
        ("Promote PR #372 to main", expected_body)
    )
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
        staticmethod(lambda *_args, **_kwargs: first_initiative),
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
    ("first_out_of_order", "second_out_of_order"),
    [(False, True), (True, False)],
    ids=("exact-to-out-of-order", "out-of-order-to-exact"),
)
def test_promote_rejects_reuse_when_promotion_mode_conflicts(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    first_out_of_order: bool,
    second_out_of_order: bool,
) -> None:
    initiative = "promotion-mode-identity-collision"
    monkeypatch.setattr(
        WorktreeService,
        "_promotion_initiative",
        staticmethod(lambda *_args, **_kwargs: initiative),
    )

    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=first_out_of_order,
        apply=True,
    )
    assert first.decision == "ready"

    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=second_out_of_order,
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_lease_conflict"


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


def test_promote_requires_merge_sha_for_every_multi_source(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_followup_source()
    promotion_harness.github.prs[source.number] = replace(
        source,
        merge_commit_sha=None,
    )

    result = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        apply=False,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_invalid_oid"


def test_promote_applies_ordered_sources_across_staging_history_gap(
    promotion_harness: PromotionHarness,
) -> None:
    previous = promotion_harness.github.prs[372]
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    (promotion_harness.repo / "unpromoted.txt").write_text(
        "staging only\n", encoding="utf-8"
    )
    git_command(promotion_harness.repo, "add", "unpromoted.txt")
    git_command(
        promotion_harness.repo,
        "commit",
        "-q",
        "-m",
        "unselected staging change",
    )
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    source = promotion_harness.add_followup_source()
    assert source.base_sha != previous.merge_commit_sha

    result = promotion_harness.service.promote(
        source_pr=(372, 373),
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    worktree = result.lease.worktree_path
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == (
        "feature updated\n"
    )
    assert (worktree / "followup.txt").read_text(encoding="utf-8") == "followup\n"
    assert not (worktree / "unpromoted.txt").exists()


def test_promote_rejects_sources_outside_staging_merge_order(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.add_followup_source()

    result = promotion_harness.service.promote(
        source_pr=(373, 372),
        target_branch="main",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_sequence_order"


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
    assert result.blockers[0]["code"] == "promotion_apply_failed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert result.lease.resolution_state is ResolutionState.NONE
    assert result.lease.conflicted_paths == ()
    assert result.lease.worktree_path.exists()
    assert promotion_harness.github.create_calls == []


def _failed_empty_exact_promotion(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    legacy: bool = False,
) -> Lease:
    def fail_apply(_worktree: Path, _patch: bytes) -> None:
        raise GitPatchConflict(("feature.txt",), "synthetic exact apply failure")

    monkeypatch.setattr(
        promotion_harness.git, "apply_indexed_patch", fail_apply
    )
    result = promotion_harness.service.promote(
        source_pr=372, target_branch="main", apply=True
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_apply_failed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert result.lease.promotion_mode is PromotionMode.EXACT
    assert result.lease.target_base_sha == result.lease.head_sha
    assert promotion_harness.git.status_porcelain(result.lease.worktree_path) == ()
    if not legacy:
        return result.lease
    with closing(promotion_harness.registry._connect()) as connection:
        connection.execute(
            "UPDATE worktree_leases SET target_base_sha = NULL WHERE id = ?",
            (result.lease.id,),
        )
        connection.commit()
    legacy_lease = promotion_harness.registry.get_lease(result.lease.id)
    assert legacy_lease is not None
    assert legacy_lease.target_base_sha is None
    return legacy_lease


def test_discard_promotion_previews_and_removes_empty_legacy_exact_lease(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(
        promotion_harness, monkeypatch, legacy=True
    )
    before = promotion_harness.registry.get_lease(lease.id)
    assert before is not None
    before_events = promotion_harness.registry.list_events(lease.id)

    preview = promotion_harness.service.discard_promotion(lease.id)

    assert preview.decision == "preview"
    assert preview.lease == before
    assert preview.actions == (
        {
            "kind": "remove_worktree",
            "lease_id": lease.id,
            "path": str(lease.worktree_path),
            "branch": lease.branch,
        },
        {
            "kind": "delete_local_branch",
            "lease_id": lease.id,
            "path": str(lease.worktree_path),
            "branch": lease.branch,
        },
    )
    assert promotion_harness.registry.get_lease(lease.id) == before
    assert promotion_harness.registry.list_events(lease.id) == before_events

    removed = promotion_harness.service.discard_promotion(lease.id, apply=True)

    assert removed.decision == "removed"
    assert [action["kind"] for action in removed.actions] == [
        "remove_worktree",
        "delete_local_branch",
    ]
    assert not lease.worktree_path.exists()
    stored = promotion_harness.registry.get_lease(lease.id)
    assert stored is not None
    assert stored.state is LeaseState.REMOVED
    with pytest.raises(GitError):
        promotion_harness.git.resolve_ref(lease.branch)


def test_discard_promotion_rejects_exact_failure_after_commit(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(
        verify_production=(
            (sys.executable, "-c", "import sys; sys.exit(1)"),
        )
    )

    failed = promotion_harness.service.promote(
        source_pr=372, target_branch="main", apply=True
    )

    assert failed.status == "blocked"
    assert failed.blockers[0]["code"] == "promotion_apply_failed"
    assert failed.lease is not None
    assert failed.lease.target_base_sha is not None
    assert failed.lease.head_sha != failed.lease.target_base_sha

    result = promotion_harness.service.discard_promotion(failed.lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "target_base_mismatch"
    assert failed.lease.worktree_path.exists()


def test_discard_promotion_rejects_wrong_failure_event(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)
    promotion_harness.registry.record_cleanup_event(
        lease.id,
        event_type="promotion_publish_failed",
        summary="promotion publication failed after apply",
    )

    result = promotion_harness.service.discard_promotion(lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_failure_not_recorded"
    assert lease.worktree_path.exists()


def test_discard_promotion_reports_remote_lookup_failure_as_external_error(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)

    def fail_remote_lookup(_branch: str) -> str | None:
        raise GitRemoteError("origin temporarily unavailable")

    monkeypatch.setattr(
        promotion_harness.git, "remote_branch_sha", fail_remote_lookup
    )

    result = promotion_harness.service.discard_promotion(lease.id)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "remote_branch_inspection_failed"
    assert lease.worktree_path.exists()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("dirty", "dirty_worktree"),
        ("head", "head_mismatch"),
        ("remote", "remote_branch_present"),
    ],
)
def test_discard_promotion_rejects_worktree_or_branch_drift(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    code: str,
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)
    if mutation == "dirty":
        (lease.worktree_path / "local.txt").write_text("dirty\n", encoding="utf-8")
    elif mutation == "head":
        (lease.worktree_path / "drift.txt").write_text("drift\n", encoding="utf-8")
        git_command(lease.worktree_path, "add", "drift.txt")
        git_command(lease.worktree_path, "commit", "-q", "-m", "promotion drift")
    else:
        git_command(
            lease.worktree_path, "push", "-q", "-u", "origin", lease.branch
        )

    result = promotion_harness.service.discard_promotion(lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == code
    assert lease.worktree_path.exists()


@pytest.mark.parametrize(
    ("invalid_fields", "code"),
    [
        ({"target_pr": 900}, "target_pr_present"),
        ({"promotion_mode": PromotionMode.OUT_OF_ORDER}, "not_exact_promotion"),
        ({"purpose": Purpose.FEATURE}, "not_promotion_lease"),
        ({"target_base_sha": "0" * 40}, "target_base_mismatch"),
    ],
)
def test_discard_promotion_rejects_pr_linked_non_exact_or_feature_lease(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    invalid_fields: dict[str, object],
    code: str,
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)
    invalid = replace(lease, **invalid_fields)
    monkeypatch.setattr(
        promotion_harness.registry,
        "get_lease_read_only",
        lambda _lease_id: invalid,
    )

    result = promotion_harness.service.discard_promotion(lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == code
    assert lease.worktree_path.exists()


def test_discard_promotion_blocks_when_cleanup_reservation_fails(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)

    def fail_reservation(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated reservation race")

    monkeypatch.setattr(
        promotion_harness.registry, "reserve_cleanup", fail_reservation
    )

    result = promotion_harness.service.discard_promotion(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_conflict"
    assert lease.worktree_path.exists()
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None


def test_discard_promotion_releases_reservation_when_worktree_removal_fails(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)
    original_remove = promotion_harness.git.remove_worktree

    def fail_remove(_path: Path) -> None:
        raise GitError("simulated worktree removal failure")

    monkeypatch.setattr(promotion_harness.git, "remove_worktree", fail_remove)

    result = promotion_harness.service.discard_promotion(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "worktree_remove_failed"
    assert lease.worktree_path.exists()
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.BLOCKED
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None

    monkeypatch.setattr(promotion_harness.git, "remove_worktree", original_remove)
    retried = promotion_harness.service.discard_promotion(lease.id, apply=True)

    assert retried.decision == "removed"
    assert not lease.worktree_path.exists()


def test_discard_promotion_recovers_completed_worktree_removal_after_restart(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)
    reservation = promotion_harness.registry.reserve_cleanup(
        lease.id, expected_version=lease.version, branch_sha=lease.head_sha
    )
    promotion_harness.git.remove_worktree(lease.worktree_path)

    result = promotion_harness.service.discard_promotion(lease.id, apply=True)

    assert result.decision == "removed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.REMOVED
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None
    assert reservation.branch_sha == lease.head_sha


def test_discard_promotion_releases_interrupted_reservation_when_path_remains(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)
    promotion_harness.registry.reserve_cleanup(
        lease.id, expected_version=lease.version, branch_sha=lease.head_sha
    )

    result = promotion_harness.service.discard_promotion(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "worktree_remove_failed"
    assert lease.worktree_path.exists()
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None


def test_discard_promotion_completes_partial_worktree_removal(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _failed_empty_exact_promotion(promotion_harness, monkeypatch)
    original_remove = promotion_harness.git.remove_worktree

    def remove_then_fail(path: Path) -> None:
        original_remove(path)
        raise GitError("simulated post-removal failure")

    monkeypatch.setattr(
        promotion_harness.git, "remove_worktree", remove_then_fail
    )

    result = promotion_harness.service.discard_promotion(lease.id, apply=True)

    assert result.decision == "removed"
    assert not lease.worktree_path.exists()
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.REMOVED
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None


def test_promote_out_of_order_skips_path_already_at_reviewed_head(
    tmp_path: Path,
) -> None:
    harness = PromotionHarness.create(tmp_path, aggregate=True)
    git_command(harness.repo, "checkout", "-q", "main")
    (harness.repo / "support.txt").write_text("support\n", encoding="utf-8")
    git_command(harness.repo, "add", "support.txt")
    git_command(harness.repo, "commit", "-q", "-m", "production support prerequisite")
    git_command(harness.repo, "push", "-q", "origin", "main")
    git_command(harness.repo, "checkout", "-q", "staging")

    result = harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert (result.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "feature\n"
    assert (result.lease.worktree_path / "support.txt").read_text(
        encoding="utf-8"
    ) == "support\n"
    assert harness.git.changed_paths(
        result.lease.worktree_path,
        result.lease.target_base_sha or "",
    ) == ("feature.txt",)


def test_promote_out_of_order_preserves_three_way_conflict_for_manual_resolution(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "out_of_order_conflict"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert result.lease.resolution_state is ResolutionState.PENDING
    assert result.lease.conflicted_paths == ("feature.txt",)
    assert result.lease.conflict_source_ordinal == 0
    persisted = promotion_harness.registry.get_lease(result.lease.id)
    assert persisted is not None
    assert persisted.state is LeaseState.BLOCKED
    assert persisted.resolution_state is ResolutionState.PENDING
    assert persisted.conflicted_paths == ("feature.txt",)
    assert persisted.conflict_source_ordinal == 0
    events = promotion_harness.registry.list_events(persisted.id)
    assert [event.event_type for event in events] == ["promotion_blocked"]
    assert events[0].summary.startswith(
        "out_of_order_conflict: reviewed patch requires managed resolution"
    )
    assert (persisted.worktree_path / "feature.txt").exists()
    assert promotion_harness.git.unmerged_paths(persisted.worktree_path) == (
        "feature.txt",
    )
    assert promotion_harness.git.remote_branch_sha(persisted.branch) is None
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_preview_reports_pending_manual_resolution_without_mutation(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert first.lease is not None
    conflict_file = first.lease.worktree_path / "feature.txt"
    conflict_file.write_text("manually resolved\n", encoding="utf-8")
    before = promotion_harness.registry.get_lease(first.lease.id)
    before_events = promotion_harness.registry.list_events(first.lease.id)

    preview = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert preview.decision == "preview"
    assert preview.lease == before
    assert preview.actions == (
        {
            "kind": "resolve_out_of_order_conflict",
            "lease_id": first.lease.id,
            "path": str(first.lease.worktree_path),
            "conflicted_paths": ["feature.txt"],
            "reviewed_paths": ["feature.txt"],
            "current_changed_paths": ["feature.txt"],
            "conflict_source_ordinal": 0,
            "remaining_source_prs": [],
        },
        {"kind": "stage_paths", "paths": ["feature.txt"]},
        {
            "kind": "commit",
            "resolution_state": "manual-reviewed",
        },
        {
            "kind": "verify_production",
            "argv": list(promotion_harness.config.verify_production[0]),
        },
        {
            "kind": "push_branch",
            "branch": first.lease.branch,
        },
        {
            "kind": "open_pull_request",
            "head": first.lease.branch,
            "base": "main",
        },
    )
    assert promotion_harness.registry.get_lease(first.lease.id) == before
    assert promotion_harness.registry.list_events(first.lease.id) == before_events
    assert conflict_file.read_text(encoding="utf-8") == "manually resolved\n"
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_applies_manual_resolution_and_opens_one_pr(
    promotion_harness: PromotionHarness,
) -> None:
    prepare_count = promotion_harness.state_dir / "manual-prepare-count.txt"
    verify_count = promotion_harness.state_dir / "manual-verify-count.txt"
    prepare_script = (
        "from pathlib import Path; "
        f"path = Path({str(prepare_count)!r}); "
        "count = int(path.read_text()) if path.exists() else 0; "
        "path.write_text(str(count + 1))"
    )
    verify_script = (
        "from pathlib import Path; "
        f"prepared = Path({str(prepare_count)!r}); "
        "assert prepared.read_text() == '1'; "
        f"path = Path({str(verify_count)!r}); "
        "count = int(path.read_text()) if path.exists() else 0; "
        "path.write_text(str(count + 1))"
    )
    promotion_harness.configure(
        prepare_command=(sys.executable, "-c", prepare_script),
        verify_production=((sys.executable, "-c", verify_script),),
    )
    promotion_harness.make_target_conflict()
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert first.lease is not None
    (first.lease.worktree_path / "feature.txt").write_text(
        "manually resolved  \n", encoding="utf-8"
    )

    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.id == first.lease.id
    assert resumed.lease.state is LeaseState.PR_OPEN
    assert resumed.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert resumed.lease.target_pr == 900
    assert resumed.lease.conflicted_paths == ("feature.txt",)
    assert (resumed.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "manually resolved  \n"
    assert promotion_harness.git.commit_message(resumed.lease.worktree_path) == "\n\n".join(
        (
            "Promote PR #372 to main",
            "\n".join(
                (
                    "AWF-Source-PR: 372",
                    f"AWF-Source-Base: {promotion_harness.source_base_sha}",
                    f"AWF-Source-Head: {promotion_harness.source_head_sha}",
                    (
                        "AWF-Source-Merge: "
                        f"{promotion_harness.github.prs[372].merge_commit_sha}"
                    ),
                    f"AWF-Target-Base: {first.lease.target_base_sha}",
                    f"AWF-Lease-ID: {first.lease.id}",
                    "AWF-Promotion-Mode: out-of-order",
                    "AWF-Resolution: manual-reviewed",
                )
            ),
        )
    )
    assert prepare_count.read_text(encoding="utf-8") == "1"
    assert verify_count.read_text(encoding="utf-8") == "2"
    assert promotion_harness.github.prs[900].head_sha == resumed.lease.head_sha
    assert len(promotion_harness.github.create_calls) == 1
    assert [
        event.event_type
        for event in promotion_harness.registry.list_events(resumed.lease.id)
    ] == [
        "promotion_blocked",
        "promotion_manual_resolution_committed",
        "promotion_publish_pending",
        "promotion_pr_reconciled",
    ]
    reused = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert reused.decision == "reuse"
    assert reused.lease == resumed.lease
    assert len(promotion_harness.github.create_calls) == 1

def _pending_out_of_order_conflict(promotion_harness: PromotionHarness) -> Lease:
    promotion_harness.make_target_conflict()
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert first.lease is not None
    assert first.lease.resolution_state is ResolutionState.PENDING
    return first.lease


def _manual_reviewed_out_of_order_conflict(
    promotion_harness: PromotionHarness,
) -> Lease:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "import sys; sys.exit(1)"),)
    )
    failed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert failed.status == "blocked"
    assert failed.lease is not None
    assert failed.lease.state is LeaseState.BLOCKED
    assert failed.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    return failed.lease


def test_recover_promotion_previews_allowed_post_commit_resolution(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "amended resolution\n", encoding="utf-8"
    )
    before_head = promotion_harness.git.head_sha(lease.worktree_path)
    before = promotion_harness.registry.get_lease(lease.id)
    before_events = promotion_harness.registry.list_events(lease.id)

    preview = promotion_harness.service.recover_promotion(lease.id)

    assert preview.decision == "preview"
    assert preview.lease == before
    assert preview.actions == (
        {
            "kind": "stage_paths",
            "lease_id": lease.id,
            "paths": ["feature.txt"],
            "dirty_paths": ["feature.txt"],
        },
        {
            "kind": "amend_manual_resolution",
            "lease_id": lease.id,
            "path": str(lease.worktree_path),
        },
    )
    assert promotion_harness.git.head_sha(lease.worktree_path) == before_head
    assert promotion_harness.registry.get_lease(lease.id) == before
    assert promotion_harness.registry.list_events(lease.id) == before_events
    assert promotion_harness.github.create_calls == []


def test_recover_promotion_lazily_backfills_verified_legacy_source_pin(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            "DELETE FROM promotion_lease_sources WHERE lease_id = ?", (lease.id,)
        )
    (lease.worktree_path / "feature.txt").write_text(
        "amended legacy resolution\n", encoding="utf-8"
    )

    preview = promotion_harness.service.recover_promotion(lease.id)

    assert preview.decision == "preview"
    assert promotion_harness.registry.get_promotion_sources_read_only(lease.id) == ()
    recovered = promotion_harness.service.recover_promotion(lease.id, apply=True)
    assert recovered.decision == "recovered"
    assert promotion_harness.registry.get_promotion_sources(lease.id) == (
        promotion_harness.service._promotion_source_pins(
            lease, (promotion_harness.github.prs[372],)
        )
    )


def test_recover_promotion_accepts_verified_legacy_three_trailer_commit(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    source = promotion_harness.github.prs[372]
    legacy_message = promotion_harness.service._promotion_message(
        sources=(source,),
        excluded_paths=(),
        target_sha=lease.target_base_sha or "",
        lease=lease,
        target_branch="main",
        resolution_state=ResolutionState.MANUAL_REVIEWED,
        include_source_merge=False,
    )
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            "DELETE FROM promotion_lease_sources WHERE lease_id = ?", (lease.id,)
        )
    git_command(
        lease.worktree_path,
        "commit",
        "--amend",
        "-q",
        "-m",
        legacy_message,
    )

    preview = promotion_harness.service.recover_promotion(lease.id)

    assert preview.decision == "preview"
    assert preview.actions[0]["kind"] == "reconcile_promotion_head"
    assert promotion_harness.registry.get_promotion_sources_read_only(lease.id) == ()
    recovered = promotion_harness.service.recover_promotion(lease.id, apply=True)
    assert recovered.decision == "reconciled"
    assert recovered.lease is not None
    assert recovered.lease.legacy_source_trailers is True
    assert recovered.lease.conflict_source_ordinal is None
    assert promotion_harness.git.commit_message(recovered.lease.worktree_path) == (
        legacy_message
    )
    assert promotion_harness.registry.get_promotion_sources(lease.id) == (
        promotion_harness.service._promotion_source_pins(lease, (source,))
    )
    resumed = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.legacy_source_trailers is True


def test_recover_promotion_amends_without_publication_then_promotes(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    message = promotion_harness.git.commit_message(lease.worktree_path)
    (lease.worktree_path / "feature.txt").write_text(
        "amended resolution\n", encoding="utf-8"
    )

    recovered = promotion_harness.service.recover_promotion(lease.id, apply=True)

    assert recovered.decision == "recovered"
    assert recovered.lease is not None
    assert recovered.lease.state is LeaseState.BLOCKED
    assert recovered.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert recovered.lease.version == lease.version + 1
    assert recovered.lease.head_sha != lease.head_sha
    assert promotion_harness.git.commit_message(
        recovered.lease.worktree_path
    ) == message
    assert promotion_harness.git.commit_parents(recovered.lease.head_sha) == (
        recovered.lease.target_base_sha,
    )
    assert promotion_harness.git.status_porcelain(recovered.lease.worktree_path) == ()
    assert (recovered.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "amended resolution\n"
    assert promotion_harness.git.remote_branch_sha(recovered.lease.branch) is None
    assert promotion_harness.github.create_calls == []
    assert promotion_harness.registry.list_events(recovered.lease.id)[
        -1
    ].event_type == "promotion_manual_resolution_amended"

    promoted = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert promoted.decision == "ready"
    assert promoted.lease is not None
    assert promoted.lease.state is LeaseState.PR_OPEN
    assert len(promotion_harness.github.create_calls) == 1


def test_recover_promotion_reconciles_an_amend_after_registry_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "amended resolution\n", encoding="utf-8"
    )
    original_transition = promotion_harness.registry.transition
    push_calls: list[str] = []

    def transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "promotion_manual_resolution_amended":
            raise RuntimeError("simulated registry failure")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(promotion_harness.registry, "transition", transition)
    monkeypatch.setattr(
        promotion_harness.git,
        "push_branch",
        lambda _worktree, branch: push_calls.append(branch),
    )

    failed = promotion_harness.service.recover_promotion(lease.id, apply=True)

    assert failed.status == "blocked"
    assert failed.blockers[0]["code"] == "promotion_recovery_failed"
    amended_head = promotion_harness.git.head_sha(lease.worktree_path)
    assert amended_head != lease.head_sha
    assert promotion_harness.registry.get_lease(lease.id) == lease
    assert push_calls == []
    assert promotion_harness.github.create_calls == []
    monkeypatch.setattr(
        promotion_harness.registry,
        "transition",
        original_transition,
    )

    preview = promotion_harness.service.recover_promotion(lease.id)
    reconciled = promotion_harness.service.recover_promotion(lease.id, apply=True)

    assert preview.actions == (
        {
            "kind": "reconcile_promotion_head",
            "lease_id": lease.id,
            "head_sha": amended_head,
        },
    )
    assert reconciled.decision == "reconciled"
    assert reconciled.lease is not None
    assert reconciled.lease.head_sha == amended_head
    assert reconciled.lease.version == lease.version + 1
    assert promotion_harness.registry.list_events(reconciled.lease.id)[
        -1
    ].event_type == "promotion_manual_resolution_amend_reconciled"
    assert push_calls == []
    assert promotion_harness.github.create_calls == []


def test_recover_promotion_reconciles_after_post_amend_remote_blocker(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "amended resolution\n", encoding="utf-8"
    )
    original_amend = promotion_harness.git.amend_commit_no_edit
    original_remote_branch_sha = promotion_harness.git.remote_branch_sha
    push_calls: list[str] = []
    amended = False

    def amend_commit_no_edit(worktree: Path) -> str:
        nonlocal amended
        head_sha = original_amend(worktree)
        amended = True
        return head_sha

    def remote_branch_sha(branch: str) -> str | None:
        if branch == lease.branch and amended:
            return "e" * 40
        return original_remote_branch_sha(branch)

    monkeypatch.setattr(
        promotion_harness.git,
        "amend_commit_no_edit",
        amend_commit_no_edit,
    )
    monkeypatch.setattr(
        promotion_harness.git,
        "remote_branch_sha",
        remote_branch_sha,
    )
    monkeypatch.setattr(
        promotion_harness.git,
        "push_branch",
        lambda _worktree, branch: push_calls.append(branch),
    )

    failed = promotion_harness.service.recover_promotion(lease.id, apply=True)

    assert failed.status == "blocked"
    assert failed.blockers[0]["code"] == "promotion_incomplete"
    amended_head = promotion_harness.git.head_sha(lease.worktree_path)
    assert amended_head != lease.head_sha
    assert promotion_harness.registry.get_lease(lease.id) == lease
    assert push_calls == []
    assert promotion_harness.github.create_calls == []
    amended = False

    reconciled = promotion_harness.service.recover_promotion(lease.id, apply=True)

    assert reconciled.decision == "reconciled"
    assert reconciled.lease is not None
    assert reconciled.lease.head_sha == amended_head
    assert push_calls == []
    assert promotion_harness.github.create_calls == []


def test_recover_promotion_rejects_index_tampering_before_amend(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "amended resolution\n", encoding="utf-8"
    )
    original_amend = promotion_harness.git.amend_commit_no_edit
    push_calls: list[str] = []

    def amend_commit_no_edit(worktree: Path) -> str:
        (worktree / "feature.txt").write_text(
            "tampered resolution\n", encoding="utf-8"
        )
        promotion_harness.git.stage_paths(worktree, ("feature.txt",))
        return original_amend(worktree)

    monkeypatch.setattr(
        promotion_harness.git,
        "amend_commit_no_edit",
        amend_commit_no_edit,
    )
    monkeypatch.setattr(
        promotion_harness.git,
        "push_branch",
        lambda _worktree, branch: push_calls.append(branch),
    )

    result = promotion_harness.service.recover_promotion(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_incomplete"
    assert promotion_harness.registry.get_lease(lease.id) == lease
    assert push_calls == []
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize(
    ("drift", "blocker"),
    (
        ("head", "promotion_provenance_changed"),
        ("checks", "source_pr_checks_failed"),
        ("paths", "promotion_provenance_changed"),
    ),
)
def test_recover_promotion_revalidates_live_source_before_mutation(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    blocker: str,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "amended resolution\n", encoding="utf-8"
    )
    source = promotion_harness.github.prs[372]
    if drift == "head":
        promotion_harness.github.prs[372] = replace(
            source,
            head_sha=source.base_sha,
        )
    elif drift == "checks":
        promotion_harness.github.prs[372] = replace(
            source,
            checks_passed=False,
        )
    else:
        promotion_harness.github.prs[372] = replace(
            source,
            changed_paths=("different.txt",),
        )
    before_head = promotion_harness.git.head_sha(lease.worktree_path)
    before = promotion_harness.registry.get_lease(lease.id)
    push_calls: list[str] = []
    monkeypatch.setattr(
        promotion_harness.git,
        "push_branch",
        lambda _worktree, branch: push_calls.append(branch),
    )

    result = promotion_harness.service.recover_promotion(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == blocker
    assert promotion_harness.git.head_sha(lease.worktree_path) == before_head
    assert promotion_harness.registry.get_lease(lease.id) == before
    assert push_calls == []
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize(
    ("violation", "blocker"),
    (
        ("outside_path", "promotion_resolution_scope_mismatch"),
        ("conflict_marker", "promotion_incomplete"),
        ("unmerged", "promotion_resolution_unmerged"),
        ("empty_dirty", "promotion_incomplete"),
        ("target_drift", "promotion_provenance_changed"),
        ("protected_entry", "promotion_resolution_scope_mismatch"),
        ("remote_branch", "promotion_incomplete"),
        ("target_pr", "promotion_incomplete"),
        ("invalid_state", "promotion_incomplete"),
    ),
)
def test_recover_promotion_blocks_invalid_manual_resolution(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    violation: str,
    blocker: str,
) -> None:
    lease = _manual_reviewed_out_of_order_conflict(promotion_harness)
    if violation != "empty_dirty":
        (lease.worktree_path / "feature.txt").write_text(
            (
                "<<<<<<< ours\namended\n=======\ntheirs\n>>>>>>> theirs\n"
                if violation == "conflict_marker"
                else "amended resolution\n"
            ),
            encoding="utf-8",
        )
    if violation == "outside_path":
        (lease.worktree_path / "outside.txt").write_text(
            "outside\n", encoding="utf-8"
        )
    elif violation == "unmerged":
        monkeypatch.setattr(
            promotion_harness.git,
            "unmerged_paths",
            lambda _worktree: ("feature.txt",),
        )
    elif violation == "target_drift":
        original_remote_branch_sha = promotion_harness.git.remote_branch_sha

        def remote_branch_sha(branch: str) -> str | None:
            if branch == "main":
                return "d" * 40
            return original_remote_branch_sha(branch)

        monkeypatch.setattr(
            promotion_harness.git,
            "remote_branch_sha",
            remote_branch_sha,
        )
    elif violation == "protected_entry":
        lease = promotion_harness.registry.transition(
            lease.id,
            LeaseState.BLOCKED,
            expected_version=lease.version,
            protected_index_entries=(("feature.txt", None),),
        )
    elif violation == "remote_branch":
        original_remote_branch_sha = promotion_harness.git.remote_branch_sha

        def remote_branch_sha(branch: str) -> str | None:
            if branch == "main":
                return lease.target_base_sha
            return "e" * 40

        monkeypatch.setattr(
            promotion_harness.git,
            "remote_branch_sha",
            remote_branch_sha,
        )
    elif violation == "target_pr":
        promotion_harness.github.open_prs[(lease.branch, "main")] = replace(
            promotion_harness.github.prs[372],
            number=901,
            state="OPEN",
            base_ref="main",
            head_ref=lease.branch,
            head_sha=lease.head_sha,
            changed_paths=(),
        )
    elif violation == "invalid_state":
        lease = promotion_harness.registry.transition(
            lease.id,
            LeaseState.ACTIVE,
            expected_version=lease.version,
        )
    before = promotion_harness.registry.get_lease(lease.id)
    before_events = promotion_harness.registry.list_events(lease.id)

    result = promotion_harness.service.recover_promotion(lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == blocker
    assert result.lease == before
    assert promotion_harness.registry.get_lease(lease.id) == before
    assert promotion_harness.registry.list_events(lease.id) == before_events
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_manual_resolution_blocks_outside_reviewed_path(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    (lease.worktree_path / "outside.txt").write_text("outside\n", encoding="utf-8")

    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_resolution_scope_mismatch"
    assert resumed.lease == lease
    assert promotion_harness.github.create_calls == []
    assert promotion_harness.git.remote_branch_sha(lease.branch) is None


@pytest.mark.parametrize(
    ("drift", "blocker"),
    (
        ("source", "promotion_provenance_changed"),
        ("target", "promotion_provenance_changed"),
    ),
)
def test_promote_out_of_order_manual_resolution_blocks_source_or_target_drift(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    blocker: str,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    original_fetch = promotion_harness.git.fetch_ref

    def drifted_fetch(ref: str) -> str:
        if drift == "source" and ref == promotion_harness.source_head_sha:
            return "f" * 40
        if drift == "target" and ref == "main":
            return "e" * 40
        return original_fetch(ref)

    monkeypatch.setattr(promotion_harness.git, "fetch_ref", drifted_fetch)
    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == blocker
    assert resumed.lease == lease
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_manual_resolution_blocks_staged_conflict_marker(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "<<<<<<< ours\nmanual\n=======\ntheirs\n>>>>>>> theirs\n",
        encoding="utf-8",
    )

    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_incomplete"
    assert resumed.lease == lease
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_manual_resolution_blocks_empty_delta(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text("target\n", encoding="utf-8")

    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_incomplete"
    assert resumed.lease == lease
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize("published", ("branch", "pull_request"))
def test_promote_out_of_order_manual_resolution_blocks_published_target(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    published: str,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    if published == "branch":
        monkeypatch.setattr(
            promotion_harness.git,
            "remote_branch_sha",
            lambda _branch: "a" * 40,
        )
    else:
        promotion_harness.github.open_prs[(lease.branch, "main")] = replace(
            promotion_harness.github.prs[372],
            number=901,
            state="OPEN",
            base_ref="main",
            head_ref=lease.branch,
            head_sha=lease.head_sha,
            changed_paths=(),
        )

    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_incomplete"
    assert resumed.lease == lease
    assert promotion_harness.github.create_calls == []
@pytest.mark.parametrize(
    ("violation", "blocker"),
    (
        ("worktree", "promotion_incomplete"),
        ("source_provenance", "promotion_provenance_changed"),
        ("target_remote_drift", "promotion_provenance_changed"),
        ("target_unavailable", "target_ref_unavailable"),
        ("remote_branch", "promotion_incomplete"),
        ("target_pr", "promotion_incomplete"),
        ("outside_path", "promotion_resolution_scope_mismatch"),
        ("unmerged_path", "promotion_resolution_scope_mismatch"),
        ("unstaged_error", "promotion_incomplete"),
    ),
)
def test_promote_out_of_order_resolution_preview_blocks_invalid_pending_lease(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    violation: str,
    blocker: str,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    if violation == "worktree":
        monkeypatch.setattr(promotion_harness.git, "list_worktrees", lambda: ())
    elif violation == "source_provenance":
        promotion_harness.github.prs[372] = replace(
            promotion_harness.github.prs[372],
            base_sha="e" * 40,
            head_sha="f" * 40,
        )
    elif violation == "target_remote_drift":
        original_remote_branch_sha = promotion_harness.git.remote_branch_sha

        def remote_branch_sha(branch: str) -> str | None:
            if branch == "main":
                return "d" * 40
            return original_remote_branch_sha(branch)

        monkeypatch.setattr(
            promotion_harness.git,
            "resolve_ref",
            lambda _ref: lease.target_base_sha,
        )
        monkeypatch.setattr(
            promotion_harness.git,
            "remote_branch_sha",
            remote_branch_sha,
        )
    elif violation == "target_unavailable":
        original_remote_branch_sha = promotion_harness.git.remote_branch_sha
        monkeypatch.setattr(
            promotion_harness.git,
            "remote_branch_sha",
            lambda branch: (
                None
                if branch == "main"
                else original_remote_branch_sha(branch)
            ),
        )
    elif violation == "remote_branch":
        def remote_branch_sha(branch: str) -> str | None:
            return lease.target_base_sha if branch == "main" else "a" * 40

        monkeypatch.setattr(
            promotion_harness.git,
            "remote_branch_sha",
            remote_branch_sha,
        )
    elif violation == "target_pr":
        promotion_harness.github.open_prs[(lease.branch, "main")] = replace(
            promotion_harness.github.prs[372],
            number=901,
            state="OPEN",
            base_ref="main",
            head_ref=lease.branch,
            head_sha=lease.head_sha,
            changed_paths=(),
        )
    elif violation == "unmerged_path":
        monkeypatch.setattr(
            promotion_harness.git,
            "unmerged_paths",
            lambda _worktree: ("feature.txt", "outside.txt"),
        )
    elif violation == "unstaged_error":
        def unstaged_paths(_worktree: Path) -> tuple[str, ...]:
            raise GitError("simulated unstaged lookup failure")

        monkeypatch.setattr(
            promotion_harness.git,
            "unstaged_paths",
            unstaged_paths,
        )
    else:
        (lease.worktree_path / "outside.txt").write_text(
            "outside\n", encoding="utf-8"
        )
    before = promotion_harness.registry.get_lease(lease.id)
    before_events = promotion_harness.registry.list_events(lease.id)

    preview = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert preview.status == "blocked"
    assert preview.blockers[0]["code"] == blocker, preview.blockers
    assert preview.lease == before
    assert promotion_harness.registry.get_lease(lease.id) == before
    assert promotion_harness.registry.list_events(lease.id) == before_events
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_preserves_clean_applied_reviewed_path(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()
    source = promotion_harness.add_followup_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.blockers[0]["code"] == "out_of_order_conflict"
    assert first.lease is not None
    assert first.lease.conflicted_paths == ("feature.txt",)
    assert first.lease.protected_index_entries == (
        promotion_harness.git.index_entry_snapshot(
            first.lease.worktree_path,
            ("followup.txt",),
        )
    )
    (first.lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )

    resumed = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert promotion_harness.git.changed_paths(
        resumed.lease.worktree_path,
        resumed.lease.target_base_sha,
        resumed.lease.head_sha,
        find_renames=True,
    ) == ("feature.txt", "followup.txt")
    assert (resumed.lease.worktree_path / "followup.txt").read_text(
        encoding="utf-8"
    ) == "followup\n"

def test_promote_out_of_order_preserves_clean_applied_symlink_entry(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()
    source = promotion_harness.add_followup_source()
    git_command(promotion_harness.repo, "checkout", "-q", source.head_ref)
    (promotion_harness.repo / "followup.txt").unlink()
    (promotion_harness.repo / "followup.txt").symlink_to("target.txt")
    git_command(promotion_harness.repo, "add", "-A", "followup.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "make followup a link")
    head_sha = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "push", "-q", "origin", source.head_ref)
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    git_command(
        promotion_harness.repo,
        "merge",
        "--no-ff",
        "-q",
        source.head_ref,
        "-m",
        "merge symlink followup",
    )
    merge_sha = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    promotion_harness.github.prs[source.number] = replace(
        source,
        head_sha=head_sha,
        merge_commit_sha=merge_sha,
    )
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.lease is not None
    assert first.lease.protected_index_entries[0][1] is not None
    assert first.lease.protected_index_entries[0][1][0] == "120000"
    (first.lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    resumed = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert (resumed.lease.worktree_path / "followup.txt").is_symlink()
    assert (resumed.lease.worktree_path / "followup.txt").readlink() == Path(
        "target.txt"
    )
    assert len(promotion_harness.github.create_calls) == 1

def test_promote_out_of_order_blocks_staged_protected_path_tampering(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()
    source = promotion_harness.add_followup_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.lease is not None
    (first.lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    (first.lease.worktree_path / "followup.txt").write_text(
        "tampered\n", encoding="utf-8"
    )
    promotion_harness.git.stage_paths(first.lease.worktree_path, ("followup.txt",))
    resumed = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_resolution_scope_mismatch"
    assert resumed.lease == first.lease
    assert promotion_harness.github.create_calls == []

def test_promote_out_of_order_blocks_protected_path_mode_tampering(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()
    source = promotion_harness.add_followup_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.lease is not None
    (first.lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    git_command(
        first.lease.worktree_path,
        "update-index",
        "--chmod=+x",
        "followup.txt",
    )
    resumed = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_resolution_scope_mismatch"
    assert resumed.lease == first.lease
    assert promotion_harness.github.create_calls == []

def test_promote_out_of_order_blocks_legacy_pending_without_protected_entries(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.make_target_conflict()
    source = promotion_harness.add_followup_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.lease is not None
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET protected_index_entries = '[]' WHERE id = ?",
            (first.lease.id,),
        )
    (first.lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    resumed = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_resolution_scope_mismatch"
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_retries_initial_live_target_lookup_error(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_remote_branch_sha = promotion_harness.git.remote_branch_sha
    unavailable = True

    def remote_branch_sha(branch: str) -> str | None:
        if branch == "main" and unavailable:
            raise GitRemoteError("origin is temporarily unavailable")
        return original_remote_branch_sha(branch)

    monkeypatch.setattr(
        promotion_harness.git,
        "remote_branch_sha",
        remote_branch_sha,
    )
    failed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert failed.status == "error"
    assert failed.lease is not None
    assert failed.lease.state is LeaseState.ACTIVE
    assert promotion_harness.github.create_calls == []
    unavailable = False
    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_out_of_order_retries_publish_after_live_target_lookup_error(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    original_remote_branch_sha = promotion_harness.git.remote_branch_sha
    unavailable = True

    def remote_branch_sha(branch: str) -> str | None:
        if branch == "main" and unavailable:
            raise GitRemoteError("origin is temporarily unavailable")
        return original_remote_branch_sha(branch)

    monkeypatch.setattr(
        promotion_harness.git,
        "remote_branch_sha",
        remote_branch_sha,
    )
    failed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert failed.status == "error"
    assert failed.lease is not None
    assert failed.lease.state is LeaseState.ACTIVE
    assert failed.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert promotion_harness.github.create_calls == []
    unavailable = False
    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert len(promotion_harness.github.create_calls) == 1
@pytest.mark.parametrize(
    ("target_state", "blocker"),
    (
        ("advanced", "promotion_provenance_changed"),
        ("missing", "target_ref_unavailable"),
    ),
)
def test_promote_out_of_order_blocks_live_target_after_initial_verification(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    target_state: str,
    blocker: str,
) -> None:
    original_verify = promotion_harness.service._verify_promotion
    original_remote_branch_sha = promotion_harness.git.remote_branch_sha
    verification_count = 0
    push_calls: list[str] = []

    def verify(worktree: Path) -> tuple[dict[str, object], ...]:
        nonlocal verification_count
        verification_count += 1
        return original_verify(worktree)

    def remote_branch_sha(branch: str) -> str | None:
        if branch == "main" and verification_count:
            return None if target_state == "missing" else "d" * 40
        return original_remote_branch_sha(branch)

    def push_branch(_worktree: Path, branch: str) -> None:
        push_calls.append(branch)

    monkeypatch.setattr(promotion_harness.service, "_verify_promotion", verify)
    monkeypatch.setattr(
        promotion_harness.git,
        "remote_branch_sha",
        remote_branch_sha,
    )
    monkeypatch.setattr(promotion_harness.git, "push_branch", push_branch)
    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == blocker
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    persisted = promotion_harness.registry.get_lease(result.lease.id)
    assert persisted is not None
    assert persisted.state is LeaseState.BLOCKED
    assert verification_count == 1
    assert push_calls == []
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_blocks_remaining_unmerged_path_after_staging(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    original_stage_paths = promotion_harness.git.stage_paths
    staged: list[tuple[str, ...]] = []

    def stage_paths(worktree: Path, paths: tuple[str, ...]) -> None:
        staged.append(paths)
        original_stage_paths(worktree, paths)

    monkeypatch.setattr(promotion_harness.git, "stage_paths", stage_paths)
    monkeypatch.setattr(
        promotion_harness.git,
        "unmerged_paths",
        lambda _worktree: ("feature.txt",),
    )
    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.status == "blocked"
    assert resumed.blockers[0]["code"] == "promotion_resolution_unmerged"
    assert resumed.lease == lease
    assert staged == [("feature.txt",)]
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize("failure", ("prepare", "verify"))
def test_promote_out_of_order_retries_manual_commit_after_verification_failure(
    promotion_harness: PromotionHarness,
    failure: str,
) -> None:
    if failure == "prepare":
        promotion_harness.configure(
            prepare_command=(sys.executable, "-c", "import sys; sys.exit(1)")
        )
    else:
        promotion_harness.configure(
            verify_production=((sys.executable, "-c", "import sys; sys.exit(1)"),)
        )
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )

    failed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert failed.status == "blocked"
    assert failed.lease is not None
    assert failed.lease.state is LeaseState.BLOCKED
    assert failed.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert failed.lease.head_sha == promotion_harness.git.head_sha(
        failed.lease.worktree_path
    )
    committed_head = failed.lease.head_sha
    promotion_harness.configure(
        prepare_command=(),
        verify_production=((sys.executable, "-c", "pass"),),
    )
    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.head_sha == committed_head
    assert resumed.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert len(promotion_harness.github.create_calls) == 1
def test_promote_exact_preview_skips_legacy_read_only_active_lookup(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def legacy_lookup(*_args: object) -> Lease | None:
        raise AssertionError("exact preview must not inspect the active-lease schema")

    monkeypatch.setattr(
        promotion_harness.registry,
        "find_active_read_only",
        legacy_lookup,
    )

    preview = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert preview.decision == "preview"
    assert not promotion_harness.registry.db_path.exists()


def test_promote_out_of_order_preview_blocks_read_only_schema_error(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def legacy_lookup(*_args: object) -> Lease | None:
        raise sqlite3.OperationalError("no such column: promotion_mode")

    monkeypatch.setattr(
        promotion_harness.registry,
        "find_active_read_only",
        legacy_lookup,
    )

    preview = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=False,
    )

    assert preview.status == "blocked"
    assert preview.blockers[0]["code"] == "registry_conflict"
    assert not promotion_harness.registry.db_path.exists()


def test_promote_out_of_order_records_sorted_special_conflict_paths(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def conflict(_worktree: Path, _patch: bytes) -> None:
        raise GitPatchConflict(("z space", "a [one]"), "synthetic conflict")

    monkeypatch.setattr(promotion_harness.git, "apply_indexed_patch", conflict)
    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.lease is not None
    assert result.lease.conflicted_paths == ("a [one]", "z space")
    assert result.blockers[0]["message"].endswith(
        "conflicted paths: 'a [one]', 'z space'"
    )
    event = promotion_harness.registry.list_events(result.lease.id)[0]
    assert event.summary.endswith(
        "conflicted paths: 'a [one]', 'z space'"
    )


def test_promote_out_of_order_reconciles_manual_commit_after_transition_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    original_transition = promotion_harness.registry.transition
    original_commit = promotion_harness.git.commit
    transition_failures = 0
    commit_messages: list[str] = []

    def transition(*args: object, **kwargs: object) -> Lease:
        nonlocal transition_failures
        if (
            kwargs.get("event_type") == "promotion_manual_resolution_committed"
            and transition_failures == 0
        ):
            transition_failures += 1
            raise RuntimeError("registry temporarily unavailable")
        return original_transition(*args, **kwargs)

    def commit(
        worktree: Path, message: str, *, allow_empty: bool = False
    ) -> str:
        commit_messages.append(message)
        return original_commit(worktree, message, allow_empty=allow_empty)

    monkeypatch.setattr(promotion_harness.registry, "transition", transition)
    monkeypatch.setattr(promotion_harness.git, "commit", commit)
    failed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert failed.status == "blocked"
    assert failed.lease is not None
    assert failed.lease.resolution_state is ResolutionState.PENDING
    committed_head = promotion_harness.git.head_sha(lease.worktree_path)
    assert committed_head != lease.head_sha
    resumed = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.head_sha == committed_head
    assert resumed.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert len(commit_messages) == 1
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_out_of_order_reconciles_remaining_sources_after_transition_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = promotion_harness.add_followup_source(change_feature=False)
    promotion_harness.make_target_conflict()
    first = promotion_harness.service.promote(
        source_pr=(372, second.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert first.lease is not None
    assert first.lease.conflict_source_ordinal == 0
    (first.lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    original_transition = promotion_harness.registry.transition
    failures = 0

    def transition(*args: object, **kwargs: object) -> Lease:
        nonlocal failures
        if (
            kwargs.get("event_type") == "promotion_manual_resolution_committed"
            and failures == 0
        ):
            failures += 1
            raise RuntimeError("registry temporarily unavailable")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(promotion_harness.registry, "transition", transition)
    failed = promotion_harness.service.promote(
        source_pr=(372, second.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert failed.status == "blocked"
    assert failed.lease is not None
    assert failed.lease.resolution_state is ResolutionState.PENDING
    resumed = promotion_harness.service.promote(
        source_pr=(372, second.number),
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.conflict_source_ordinal is None
    assert resumed.lease.protected_index_entries == (
        promotion_harness.git.index_entry_snapshot(
            resumed.lease.worktree_path, ("followup.txt",)
        )
    )


def test_promote_out_of_order_blocks_immutable_marker_commit(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    original_commit = promotion_harness.git.commit

    def commit(
        worktree: Path, message: str, *, allow_empty: bool = False
    ) -> str:
        (worktree / "feature.txt").write_text(
            "<<<<<<< ours\nmanual\n=======\ntheirs\n>>>>>>> theirs\n",
            encoding="utf-8",
        )
        promotion_harness.git.stage_paths(worktree, ("feature.txt",))
        return original_commit(worktree, message, allow_empty=allow_empty)

    monkeypatch.setattr(promotion_harness.git, "commit", commit)
    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_incomplete"
    assert result.lease == lease
    assert promotion_harness.github.create_calls == []


def test_promote_out_of_order_blocks_target_drift_after_final_verification(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _pending_out_of_order_conflict(promotion_harness)
    (lease.worktree_path / "feature.txt").write_text(
        "manually resolved\n", encoding="utf-8"
    )
    original_verify = promotion_harness.service._verify_promotion
    original_remote_branch_sha = promotion_harness.git.remote_branch_sha
    verification_count = 0
    find_counts: list[int] = []
    push_calls: list[str] = []

    def verify(worktree: Path) -> tuple[dict[str, object], ...]:
        nonlocal verification_count
        verification_count += 1
        find_counts.append(len(promotion_harness.github.find_calls))
        return original_verify(worktree)

    def remote_branch_sha(branch: str) -> str | None:
        if branch == "main" and verification_count >= 2:
            return "d" * 40
        return original_remote_branch_sha(branch)

    def push_branch(_worktree: Path, branch: str) -> None:
        push_calls.append(branch)

    monkeypatch.setattr(promotion_harness.service, "_verify_promotion", verify)
    monkeypatch.setattr(
        promotion_harness.git,
        "remote_branch_sha",
        remote_branch_sha,
    )
    monkeypatch.setattr(promotion_harness.git, "push_branch", push_branch)
    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "promotion_provenance_changed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    persisted = promotion_harness.registry.get_lease(result.lease.id)
    assert persisted is not None
    assert persisted.state is LeaseState.BLOCKED
    assert verification_count == 2
    assert find_counts == [2, 2]
    assert push_calls == []
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


def test_promote_explicit_approved_policy_rejects_unapproved_self_merge(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(source_review_policy="approved")
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
def test_promote_self_merge_policy_accepts_unapproved_self_merge(
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


def test_promote_self_merge_policy_accepts_approved_source_without_identities(
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
def test_promote_self_merge_policy_retains_source_gates(
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
def test_promote_self_merge_policy_rejects_non_self_merged_source(
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


def _blocked_clean_precommit_out_of_order_promotion(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PullRequest, Lease]:
    source = promotion_harness.add_followup_source(
        number=322,
        change_feature=False,
    )
    original_apply = promotion_harness.git.apply_indexed_patch

    def fail_before_commit(_worktree: Path, _patch: bytes) -> None:
        raise GitError("simulated pre-commit patch failure")

    monkeypatch.setattr(
        promotion_harness.git,
        "apply_indexed_patch",
        fail_before_commit,
    )
    failed = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    monkeypatch.setattr(
        promotion_harness.git,
        "apply_indexed_patch",
        original_apply,
    )

    assert failed.status == "blocked"
    assert failed.blockers[0]["code"] == "promotion_apply_failed"
    assert failed.lease is not None

    assert failed.lease.state is LeaseState.BLOCKED
    assert failed.lease.resolution_state is ResolutionState.NONE
    assert promotion_harness.git.status_porcelain(failed.lease.worktree_path) == ()
    assert (
        promotion_harness.git.head_sha(failed.lease.worktree_path)
        == failed.lease.head_sha
    )
    return source, failed.lease


def _advance_promotion_target(
    promotion_harness: PromotionHarness,
) -> str:
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "retry-prerequisite.txt").write_text(
        "target advanced\n",
        encoding="utf-8",
    )
    git_command(promotion_harness.repo, "add", "retry-prerequisite.txt")
    git_command(
        promotion_harness.repo,
        "commit",
        "-q",
        "-m",
        "advance target for promotion retry",
    )
    target_sha = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    return target_sha
def test_promote_rebuilds_out_of_order_verification_failure_after_target_advances(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "import sys; sys.exit(1)"),)
    )
    first = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "promotion_apply_failed"
    assert first.lease is not None

    promotion_harness.configure(
        verify_production=((sys.executable, "-c", "pass"),)
    )
    target_sha = _advance_promotion_target(promotion_harness)
    second = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert second.decision == "ready", second.blockers
    assert second.lease is not None
    assert second.lease.target_base_sha == target_sha
    assert promotion_harness.git.commit_parents(second.lease.head_sha) == (
        target_sha,
    )



def test_promote_retries_clean_stale_precommit_out_of_order_lease_on_advanced_target(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, stale = _blocked_clean_precommit_out_of_order_promotion(
        promotion_harness,
        monkeypatch,
    )
    stale_events = promotion_harness.registry.list_events(stale.id)
    target_sha = _advance_promotion_target(promotion_harness)

    retried = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert retried.decision == "ready"
    assert retried.lease is not None
    assert retried.lease.id != stale.id
    assert retried.lease.state is LeaseState.PR_OPEN
    assert retried.lease.initiative == (
        f"pr-{source.number}-to-main-out-of-order-retry-{target_sha}"
    )
    assert retried.lease.branch == (
        f"awf/pr-{source.number}-to-main-out-of-order-retry-{target_sha}/promote"
    )
    assert retried.lease.target_base_sha == target_sha
    assert promotion_harness.registry.get_lease(stale.id) == stale
    assert promotion_harness.registry.list_events(stale.id) == stale_events
    assert len(promotion_harness.registry.list_leases()) == 2
    assert len(promotion_harness.github.create_calls) == 1
    assert promotion_harness.github.create_calls[0]["base"] == "main"
    assert promotion_harness.github.create_calls[0]["head"] == retried.lease.branch


def test_finish_removes_only_exact_generated_out_of_order_retry_path(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _stale = _blocked_clean_precommit_out_of_order_promotion(
        promotion_harness,
        monkeypatch,
    )
    _advance_promotion_target(promotion_harness)
    retried = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )
    assert retried.lease is not None
    lease = retried.lease
    assert lease.target_base_sha is not None
    assert promotion_harness.service._cleanup_path_blocker(lease) is None

    tampered = (
        replace(lease, target_base_sha="d" * 40),
        replace(lease, initiative=f"{lease.initiative}-tampered"),
        replace(lease, branch=f"{lease.branch}-tampered"),
        replace(lease, repository_id="different-repository"),
        replace(
            lease,
            worktree_path=lease.worktree_path.parent
            / f"promotion-retry-{lease.target_base_sha}-{'0' * 16}",
        ),
    )
    for candidate in tampered:
        blocker = promotion_harness.service._cleanup_path_blocker(candidate)
        assert blocker is not None
        assert blocker["code"] in {"repository_mismatch", "unsafe_worktree_path"}

    assert lease.target_pr is not None
    target = promotion_harness.github.prs[lease.target_pr]
    promotion_harness.github.prs[lease.target_pr] = replace(
        target,
        state="MERGED",
        head_sha=lease.head_sha,
        merge_commit_sha=lease.head_sha,
    )
    promotion_harness.service.status(refresh=True)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert not lease.worktree_path.exists()


def test_promote_preview_and_apply_share_stale_retry_identity(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _stale = _blocked_clean_precommit_out_of_order_promotion(
        promotion_harness,
        monkeypatch,
    )
    target_sha = _advance_promotion_target(promotion_harness)

    preview = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=False,
    )
    applied = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert preview.decision == "preview"
    assert preview.actions[0]["target_base_sha"] == target_sha
    assert applied.decision == "ready"
    assert applied.lease is not None
    assert preview.actions[0]["branch"] == applied.lease.branch
    assert preview.actions[0]["path"] == str(applied.lease.worktree_path)


def test_promote_retries_clean_precommit_lease_without_target_advance(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, stale = _blocked_clean_precommit_out_of_order_promotion(
        promotion_harness,
        monkeypatch,
    )

    retried = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert retried.decision == "ready"
    assert retried.lease is not None
    assert retried.lease.id != stale.id
    assert retried.lease.state is LeaseState.PR_OPEN
    assert retried.lease.target_base_sha == stale.target_base_sha
    assert len(promotion_harness.registry.list_leases()) == 2


@pytest.mark.parametrize("state", ("dirty", "published"))
def test_promote_does_not_retry_dirty_or_published_stale_precommit_lease(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    source, stale = _blocked_clean_precommit_out_of_order_promotion(
        promotion_harness,
        monkeypatch,
    )
    _advance_promotion_target(promotion_harness)
    if state == "dirty":
        (stale.worktree_path / "retry-dirty.txt").write_text(
            "dirty\n",
            encoding="utf-8",
        )
    else:
        git_command(
            stale.worktree_path,
            "push",
            "-q",
            "-u",
            "origin",
            stale.branch,
        )

    retried = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        out_of_order=True,
        apply=True,
    )

    assert retried.status == "blocked"
    assert retried.blockers[0]["code"] == "promotion_incomplete"
    assert retried.lease == stale
    assert len(promotion_harness.registry.list_leases()) == 1



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
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
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



def test_promote_recovers_content_mismatch_reviewed_rename_when_disabled(
    promotion_harness: PromotionHarness,
) -> None:
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "feature.txt").write_text(
        "copy=old\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n",
        encoding="utf-8",
    )
    git_command(promotion_harness.repo, "add", "feature.txt")
    git_command(
        promotion_harness.repo, "commit", "-q", "-m", "production prerequisite old"
    )
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    (promotion_harness.repo / "feature.txt").write_text(
        "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n",
        encoding="utf-8",
    )
    git_command(promotion_harness.repo, "add", "feature.txt")
    git_command(
        promotion_harness.repo, "commit", "-q", "-m", "staging prerequisite"
    )
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    base_sha = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "checkout", "-q", "-b", "feature/pr-373")
    git_command(promotion_harness.repo, "mv", "feature.txt", "renamed.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "rename source")
    head_sha = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(
        promotion_harness.repo, "push", "-q", "-u", "origin", "feature/pr-373"
    )
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    git_command(
        promotion_harness.repo,
        "merge",
        "--no-ff",
        "-q",
        "feature/pr-373",
        "-m",
        "merge renamed source",
    )
    merge_sha = git_command(promotion_harness.repo, "rev-parse", "HEAD")
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    git_command(promotion_harness.repo, "config", "diff.renames", "false")
    source = PullRequest(
        number=373,
        state="MERGED",
        base_ref="staging",
        base_sha=base_sha,
        head_ref="feature/pr-373",
        head_sha=head_sha,
        merge_commit_sha=merge_sha,
        review_decision="APPROVED",
        checks_passed=True,
        changed_paths=("renamed.txt",),
        url="https://github.example/acme/repo/pull/373",
    )
    promotion_harness.github.prs[source.number] = source

    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
    assert first.lease is not None
    target_sha = promotion_harness.advance_target_prerequisite()

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.state is LeaseState.PR_OPEN
    assert promotion_harness.git.commit_parents(second.lease.head_sha) == (target_sha,)
    assert len(promotion_harness.github.create_calls) == 1
def test_promote_does_not_rebuild_content_mismatch_without_target_advance(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    assert any(
        worktree.path == first.lease.worktree_path
        for worktree in promotion_harness.git.list_worktrees()
    )
    assert promotion_harness.git.status_porcelain(first.lease.worktree_path) == ()
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head
    assert promotion_harness.git.commit_parents(blocked_head) == (
        promotion_harness.git.resolve_ref("main"),
    )


    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert second.blockers[0]["message"] == (
        f"lease {first.lease.id} target branch has not advanced"
    )

    assert promotion_harness.github.create_calls == []
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head


def test_promote_does_not_rebuild_dirty_content_mismatch_after_target_advances(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    recorded_target_sha = promotion_harness.git.commit_parents(blocked_head)[0]
    target_sha = promotion_harness.advance_target_prerequisite()
    assert target_sha != recorded_target_sha
    assert any(
        worktree.path == first.lease.worktree_path
        for worktree in promotion_harness.git.list_worktrees()
    )
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head
    assert promotion_harness.git.commit_parents(blocked_head) == (
        recorded_target_sha,
    )

    (first.lease.worktree_path / "README.txt").write_text(
        "dirty\n", encoding="utf-8"
    )
    assert promotion_harness.git.status_porcelain(first.lease.worktree_path)

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert second.blockers[0]["message"] == (
        f"lease {first.lease.id} was not verified for content-mismatch recovery"
    )

    assert promotion_harness.github.create_calls == []
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head


def test_promote_does_not_rebuild_content_mismatch_with_published_branch(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    promotion_harness.advance_target_prerequisite()
    git_command(
        first.lease.worktree_path,
        "push",
        "-q",
        "origin",
        first.lease.branch,
    )
    assert promotion_harness.git.remote_branch_sha(first.lease.branch) == blocked_head


    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert second.blockers[0]["message"] == (
        f"lease {first.lease.id} promotion branch is already published"
    )

    assert promotion_harness.github.create_calls == []
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head


def test_promote_does_not_rebuild_content_mismatch_with_forged_parent(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
    assert first.lease is not None
    original_message = promotion_harness.git.commit_message(first.lease.worktree_path)
    recorded_target_sha = promotion_harness.git.commit_parents(first.lease.head_sha)[
        0
    ]

    forged_parent = promotion_harness.git.resolve_ref("staging")
    promotion_tree = git_command(
        first.lease.worktree_path, "rev-parse", "HEAD^{tree}"
    )
    forged_head = git_command(
        first.lease.worktree_path,
        "commit-tree",
        promotion_tree,
        "-p",
        forged_parent,
        "-m",
        original_message,
    )
    assert forged_parent != recorded_target_sha

    git_command(
        first.lease.worktree_path, "reset", "--hard", "-q", forged_head
    )
    blocked = promotion_harness.registry.get_lease(first.lease.id)
    assert blocked is not None
    forged_lease = promotion_harness.registry.transition(
        blocked.id,
        LeaseState.BLOCKED,
        expected_version=blocked.version,
        event_type="promotion_blocked",
        summary="promotion_content_mismatch: forged promotion parent",
        head_sha=forged_head,
    )

    promotion_harness.advance_target_prerequisite()
    assert any(
        worktree.path == first.lease.worktree_path
        for worktree in promotion_harness.git.list_worktrees()
    )
    assert promotion_harness.git.status_porcelain(first.lease.worktree_path) == ()
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == forged_lease.head_sha
    assert promotion_harness.git.commit_parents(forged_head) == (forged_parent,)


    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert promotion_harness.git.commit_message(
        first.lease.worktree_path
    ) == original_message
    assert promotion_harness.git.commit_parents(forged_head) == (forged_parent,)
    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert second.blockers[0]["message"] == (
        f"lease {first.lease.id} was not verified for content-mismatch recovery"
    )

    assert promotion_harness.github.create_calls == []
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == forged_head


def test_promote_does_not_rebuild_content_mismatch_after_source_provenance_changes(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    promotion_harness.advance_target_prerequisite()
    assert source.merge_commit_sha is not None
    assert source.merge_commit_sha != source.head_sha
    assert git_command(
        promotion_harness.repo, "rev-parse", f"{source.merge_commit_sha}^{{commit}}"
    ) == source.merge_commit_sha
    promotion_harness.github.prs[source.number] = replace(
        source, head_sha=source.merge_commit_sha
    )


    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_incomplete"
    assert second.blockers[0]["message"] == (
        f"lease {first.lease.id} does not have exact promotion provenance"
    )
    assert promotion_harness.github.create_calls == []
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head

def test_promote_recovers_content_mismatch_with_fully_excluded_source(
    promotion_harness: PromotionHarness,
) -> None:
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "feature.txt").write_text(
        "copy=old\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n",
        encoding="utf-8",
    )
    git_command(promotion_harness.repo, "add", "feature.txt")
    git_command(
        promotion_harness.repo, "commit", "-q", "-m", "production prerequisite old"
    )
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    (promotion_harness.repo / "feature.txt").write_text(
        "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=old\n",
        encoding="utf-8",
    )
    git_command(promotion_harness.repo, "add", "feature.txt")
    git_command(
        promotion_harness.repo, "commit", "-q", "-m", "staging prerequisite"
    )
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    excluded = promotion_harness.add_followup_source(
        373,
        change_feature=False,
        include_followup=False,
        team_text="excluded team update\n",
    )
    source = promotion_harness.add_followup_source(
        374,
        feature_text=(
            "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=new\n"
        ),
        include_followup=False,
    )

    first = promotion_harness.service.promote(
        source_pr=(excluded.number, source.number),
        exclude_paths=("team.txt",),
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "staging_missing_main_delta"
    assert first.lease is not None
    target_sha = promotion_harness.advance_target_prerequisite()

    second = promotion_harness.service.promote(
        source_pr=(excluded.number, source.number),
        exclude_paths=("team.txt",),
        target_branch="main",
        apply=True,
    )

    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.id == first.lease.id
    assert promotion_harness.git.commit_parents(second.lease.head_sha) == (target_sha,)
    assert not (second.lease.worktree_path / "team.txt").exists()
    assert (second.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "copy=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nrank=new\n"
    assert len(promotion_harness.github.create_calls) == 1


def test_promote_reports_content_mismatch_recovery_remote_preflight_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    promotion_harness.advance_target_prerequisite()
    original_fetch_ref = promotion_harness.git.fetch_ref

    def fail_target_fetch(ref: str) -> str:
        if ref == "main":
            raise GitRemoteError("target unavailable")
        return original_fetch_ref(ref)

    monkeypatch.setattr(promotion_harness.git, "fetch_ref", fail_target_fetch)

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "error"
    assert second.blockers[0]["code"] == "promotion_recovery_failed"
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize(
    "error",
    (
        GitError("patch unavailable"),
        OSError("patch unavailable"),
        RuntimeError("patch unavailable"),
        sqlite3.Error("patch unavailable"),
    ),
)
def test_promote_blocks_content_mismatch_recovery_source_patch_failures(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    promotion_harness.advance_target_prerequisite()

    def fail_patch(*_: object, **__: object) -> bytes:
        raise error

    monkeypatch.setattr(promotion_harness.git, "binary_diff", fail_patch)

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_recovery_failed"
    assert promotion_harness.git.head_sha(first.lease.worktree_path) == blocked_head
    assert promotion_harness.github.create_calls == []


def test_promote_records_actual_head_when_content_mismatch_restore_fails(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    promotion_harness.advance_target_prerequisite()
    original_transition = promotion_harness.registry.transition

    def fail_rebuild_transition(*args: object, **kwargs: object) -> Lease:
        summary = kwargs.get("summary")
        if isinstance(summary, str) and summary.startswith(
            "promotion_verification_failed: rebuilt"
        ):
            raise RuntimeError("recording failed")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        promotion_harness.registry, "transition", fail_rebuild_transition
    )
    original_reset_hard = promotion_harness.git.reset_hard

    def fail_restore(worktree_path: Path, ref: str) -> None:
        if ref == blocked_head:
            raise GitError("restore failed")
        original_reset_hard(worktree_path, ref)

    monkeypatch.setattr(promotion_harness.git, "reset_hard", fail_restore)

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_recovery_restore_failed"
    assert "recording failed" in second.blockers[0]["message"]
    assert "restore failed" in second.blockers[0]["message"]
    reconciled = promotion_harness.registry.get_lease(first.lease.id)
    assert reconciled is not None
    assert reconciled.head_sha == promotion_harness.git.head_sha(
        first.lease.worktree_path
    )
    assert reconciled.head_sha != blocked_head
    latest = promotion_harness.registry.list_events(first.lease.id)[-1]
    assert latest.event_type == "promotion_blocked"
    assert latest.summary.startswith("promotion_recovery_restore_failed:")
    assert promotion_harness.github.create_calls == []


def test_promote_surfaces_content_mismatch_restore_reconciliation_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    promotion_harness.advance_target_prerequisite()
    original_transition = promotion_harness.registry.transition

    def fail_rebuild_and_reconciliation(
        *args: object, **kwargs: object
    ) -> Lease:
        summary = kwargs.get("summary")
        if isinstance(summary, str) and summary.startswith(
            "promotion_verification_failed: rebuilt"
        ):
            raise RuntimeError("recording failed")
        if isinstance(summary, str) and summary.startswith(
            "promotion_recovery_restore_failed:"
        ):
            raise RuntimeError("reconciliation failed")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        promotion_harness.registry, "transition", fail_rebuild_and_reconciliation
    )
    original_reset_hard = promotion_harness.git.reset_hard

    def fail_restore(worktree_path: Path, ref: str) -> None:
        if ref == blocked_head:
            raise GitError("restore failed")
        original_reset_hard(worktree_path, ref)

    monkeypatch.setattr(promotion_harness.git, "reset_hard", fail_restore)

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.status == "blocked"
    assert second.blockers[0]["code"] == "promotion_recovery_restore_failed"
    assert "recording failed" in second.blockers[0]["message"]
    assert "restore failed" in second.blockers[0]["message"]
    assert "reconciliation failed" in second.blockers[0]["message"]
    assert promotion_harness.github.create_calls == []
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
        ("deployment_unknown", "deployment_evidence_unknown"),
        ("deployment_failed", "deployment_evidence_failed"),
        ("deployment_pending", "deployment_evidence_pending"),
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


def test_finish_removes_a_verified_superseded_promotion(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    git_command(
        promotion_harness.repo,
        "checkout",
        "-q",
        "-b",
        "production-image",
        lease.head_sha,
    )
    (promotion_harness.repo / "production-image.txt").write_text(
        "current production image\n",
        encoding="utf-8",
    )
    git_command(promotion_harness.repo, "add", "production-image.txt")
    git_command(
        promotion_harness.repo,
        "commit",
        "-q",
        "-m",
        "advance production image",
    )
    production_image_git_sha = git_command(
        promotion_harness.repo, "rev-parse", "HEAD"
    )
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    promotion_harness.evidence_executor.status = "superseded_healthy"
    promotion_harness.evidence_executor.production_image_git_sha = (
        production_image_git_sha
    )

    refreshed = promotion_harness.service.status(refresh=True)
    current = promotion_harness.registry.get_lease(lease.id)

    assert refreshed.status == "ok"
    assert current is not None
    assert current.state is LeaseState.CLEANABLE
    evidence = promotion_harness.registry.list_events(lease.id)[-1].evidence
    assert evidence is not None
    assert evidence["status"] == "superseded_healthy"
    assert evidence["subject_revision"] == lease.head_sha
    assert evidence["production_image_git_sha"] == production_image_git_sha

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert not lease.worktree_path.exists()


@pytest.mark.parametrize(
    ("image_revision", "failure_code"),
    (
        ("equal", "deployment_superseded_subject_equal"),
        ("non_ancestor", "deployment_superseded_subject_not_ancestor"),
        ("unavailable", "deployment_superseded_revision_unavailable"),
    ),
)
def test_refresh_preserves_unverified_superseded_promotion(
    promotion_harness: PromotionHarness,
    image_revision: str,
    failure_code: str,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    promotion_harness.evidence_executor.status = "superseded_healthy"
    promotion_harness.evidence_executor.production_image_git_sha = {
        "equal": lease.head_sha,
        "non_ancestor": promotion_harness.source_head_sha,
        "unavailable": "d" * 40,
    }[image_revision]

    result = promotion_harness.service.status(refresh=True)
    current = promotion_harness.registry.get_lease(lease.id)

    assert result.status == "ok"
    assert current is not None
    assert current.state is LeaseState.DEPLOYING
    assert current.deployment_state is DeploymentState.UNKNOWN
    assert failure_code in {warning["code"] for warning in result.warnings}
    assert lease.worktree_path.exists()


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


def test_gc_preserves_unverified_superseded_promotion(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    promotion_harness.evidence_executor.status = "superseded_healthy"
    promotion_harness.evidence_executor.production_image_git_sha = lease.head_sha
    stale_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
        timespec="seconds"
    )
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET last_used_at = ? WHERE id = ?",
            (stale_at, lease.id),
        )

    result = promotion_harness.service.gc(
        merged=True,
        older_than="7d",
        apply=True,
    )

    assert result.status == "blocked"
    assert "deployment_superseded_subject_equal" in {
        item["code"] for item in result.blockers
    }
    assert lease.worktree_path.exists()


@pytest.mark.parametrize(
    "failure_code",
    [
        "deployment_adapter_nonzero",
        "deployment_adapter_timeout",
        "deployment_evidence_invalid",
    ],
)
def test_finish_fails_closed_when_fresh_deployment_evidence_is_invalid(
    promotion_harness: PromotionHarness,
    failure_code: str,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    promotion_harness.evidence_executor.failure_code = failure_code

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "deployment_evidence_unknown"
    assert lease.worktree_path.exists()
    current = promotion_harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.BLOCKED
    assert current.deployment_state is DeploymentState.UNKNOWN

def test_finish_requires_a_fresh_healthy_deployment_probe(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    initial_requests = len(promotion_harness.evidence_executor.requests)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert len(promotion_harness.evidence_executor.requests) == initial_requests + 2
    assert not lease.worktree_path.exists()


def test_refresh_records_healthy_evidence_directly_as_cleanable(
    promotion_harness: PromotionHarness,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    before = promotion_harness.registry.list_events(lease.id)

    result = promotion_harness.service.status(refresh=True)

    assert result.status == "ok"
    refreshed = promotion_harness.registry.list_events(lease.id)[len(before) :]
    assert len(refreshed) == 1
    assert refreshed[0].event_type == "deployment_evidence"
    assert refreshed[0].to_state == LeaseState.CLEANABLE.value
    assert refreshed[0].from_state != LeaseState.DEPLOYED.value


def test_finish_preserves_promotion_when_forced_probe_cannot_be_recorded(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_transition = promotion_harness.registry.transition

    def transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "deployment_evidence":
            raise RuntimeError("database unavailable")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(promotion_harness.registry, "transition", transition)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert "deployment_evidence_unknown" in {item["code"] for item in result.blockers}
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


def test_finish_rejects_merge_revision_changed_after_cleanup_reservation(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_reserve = promotion_harness.registry.reserve_cleanup

    def reserve_then_change(*args: object, **kwargs: object):
        reservation = original_reserve(*args, **kwargs)
        promotion_harness.github.prs[lease.target_pr] = replace(
            promotion_harness.github.prs[lease.target_pr],
            merge_commit_sha="e" * 40,
        )
        return reservation

    monkeypatch.setattr(
        promotion_harness.registry, "reserve_cleanup", reserve_then_change
    )
    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "deployment_subject_changed"
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None
    assert lease.worktree_path.exists()



def test_finish_rejects_merge_revision_changed_after_fresh_probe(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_probe = promotion_harness.service._force_cleanup_deployment_probe

    def probe_then_change(*args: object, **kwargs: object):
        result = original_probe(*args, **kwargs)
        promotion_harness.github.prs[lease.target_pr] = replace(
            promotion_harness.github.prs[lease.target_pr],
            merge_commit_sha="e" * 40,
        )
        return result

    monkeypatch.setattr(
        promotion_harness.service, "_force_cleanup_deployment_probe", probe_then_change
    )
    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "deployment_subject_changed"
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



@pytest.mark.parametrize(
    ("substitution", "blocker_code"),
    (
        ("directory", "worktree_identity_changed"),
        ("symlink", "unsafe_worktree_path"),
    ),
)
def test_finish_rechecks_worktree_identity_after_cleanup_reservation(
    promotion_harness: PromotionHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
    blocker_code: str,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    original_identity = promotion_harness.service._cleanup_worktree_identity
    relocated = tmp_path / "relocated-worktree"
    swapped = False
    remove_calls: list[Path] = []

    def recheck_identity(current: Lease, *, expected=None):
        nonlocal swapped
        if expected is not None and not swapped:
            swapped = True
            current.worktree_path.rename(relocated)
            if substitution == "symlink":
                current.worktree_path.symlink_to(relocated, target_is_directory=True)
            else:
                current.worktree_path.mkdir()
        return original_identity(current, expected=expected)

    def remove_worktree_if_called(path: Path) -> None:
        remove_calls.append(path)

    monkeypatch.setattr(
        promotion_harness.service, "_cleanup_worktree_identity", recheck_identity
    )
    monkeypatch.setattr(
        promotion_harness.git, "remove_worktree", remove_worktree_if_called
    )

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert blocker_code in {item["code"] for item in result.blockers}
    assert not remove_calls
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None
    assert relocated.is_dir()


def _record_unexpected_cleanup_sinks(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> dict[str, list[object]]:
    calls: dict[str, list[object]] = {
        "remove": [],
        "complete": [],
        "local": [],
        "remote": [],
    }

    def remove_worktree(path: Path) -> None:
        calls["remove"].append(path)
        raise AssertionError("unexpected worktree removal")

    def complete_cleanup(*args: object, **kwargs: object) -> Lease:
        calls["complete"].append((args, kwargs))
        raise AssertionError("unexpected cleanup completion")

    def delete_branch(branch: str, expected_sha: str) -> None:
        calls["local"].append((branch, expected_sha))
        raise AssertionError("unexpected local branch deletion")

    def delete_remote_branch(branch: str, expected_sha: str) -> None:
        calls["remote"].append((branch, expected_sha))
        raise AssertionError("unexpected remote branch deletion")

    monkeypatch.setattr(promotion_harness.git, "remove_worktree", remove_worktree)
    monkeypatch.setattr(
        promotion_harness.registry, "complete_cleanup", complete_cleanup
    )
    monkeypatch.setattr(promotion_harness.git, "delete_branch_if_at", delete_branch)
    monkeypatch.setattr(
        promotion_harness.git, "delete_remote_branch_if_at", delete_remote_branch
    )
    return calls


@pytest.mark.parametrize("escape", ("absolute", "parent"))
def test_finish_blocks_canonical_registry_repository_name_escape(
    promotion_harness: PromotionHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    escape: str,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    outside = tmp_path / "outside" / lease.id
    outside.parent.mkdir()
    git_command(
        promotion_harness.repo,
        "worktree",
        "move",
        str(lease.worktree_path),
        str(outside),
    )
    if escape == "absolute":
        repository_name = str(outside.parent)
        registry_path = outside
    else:
        repository_name = "../outside"
        registry_path = promotion_harness.cache_dir / ".." / "outside" / lease.id
        original_list_worktrees = promotion_harness.git.list_worktrees

        def list_worktrees() -> tuple[GitWorktree, ...]:
            return tuple(
                replace(worktree, path=registry_path)
                if worktree.path == outside
                else worktree
                for worktree in original_list_worktrees()
            )

        monkeypatch.setattr(
            promotion_harness.git, "list_worktrees", list_worktrees
        )
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            """
            UPDATE worktree_leases
            SET repository_name = ?, worktree_path = ?
            WHERE id = ?
            """,
            (repository_name, str(registry_path), lease.id),
        )
    calls = _record_unexpected_cleanup_sinks(promotion_harness, monkeypatch)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "repository_mismatch"
    assert calls == {"remove": [], "complete": [], "local": [], "remote": []}
    assert outside.is_dir()


def test_finish_blocks_foreign_repository_root_before_cleanup(
    promotion_harness: PromotionHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    promotion_harness.github.prs[lease.target_pr] = replace(
        promotion_harness.github.prs[lease.target_pr],
        state="MERGED",
        head_sha=lease.head_sha,
        merge_commit_sha=lease.head_sha,
    )
    foreign_root = tmp_path / "foreign-repository"
    foreign_root.mkdir()
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET repository_root = ? WHERE id = ?",
            (str(foreign_root), lease.id),
        )
    calls = _record_unexpected_cleanup_sinks(promotion_harness, monkeypatch)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "repository_mismatch"
    assert calls == {"remove": [], "complete": [], "local": [], "remote": []}
    assert lease.worktree_path.is_dir()


def test_finish_recovery_blocks_forged_unmerged_reservation(
    promotion_harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    promotion_harness.github.prs[lease.target_pr] = replace(
        promotion_harness.github.prs[lease.target_pr],
        state="OPEN",
        merge_commit_sha=None,
    )
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET version = version + 1 WHERE id = ?",
            (lease.id,),
        )
        connection.execute(
            """
            INSERT INTO worktree_cleanup_reservations (
                lease_id, reserved_version, branch_sha, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                lease.id,
                lease.version + 1,
                lease.head_sha,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
    promotion_harness.git.remove_worktree(lease.worktree_path)
    calls = _record_unexpected_cleanup_sinks(promotion_harness, monkeypatch)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "pr_not_merged"
    assert calls == {"remove": [], "complete": [], "local": [], "remote": []}


@pytest.mark.parametrize("mutation", ("branch_sha", "path", "version"))
def test_finish_recovery_blocks_tampered_reservation(
    promotion_harness: PromotionHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    lease = promotion_harness.merged_promotion("healthy")
    reservation = promotion_harness.registry.reserve_cleanup(
        lease.id,
        expected_version=lease.version,
        branch_sha=lease.head_sha,
    )
    with sqlite3.connect(promotion_harness.registry.db_path) as connection:
        if mutation == "branch_sha":
            connection.execute(
                """
                UPDATE worktree_cleanup_reservations
                SET branch_sha = ?
                WHERE lease_id = ?
                """,
                ("0" * 40, lease.id),
            )
        elif mutation == "path":
            connection.execute(
                "UPDATE worktree_leases SET worktree_path = ? WHERE id = ?",
                (str(tmp_path / "outside-worktree"), lease.id),
            )
        else:
            connection.execute(
                "UPDATE worktree_leases SET version = version + 1 WHERE id = ?",
                (lease.id,),
            )
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is not None
    assert reservation.branch_sha == lease.head_sha
    calls = _record_unexpected_cleanup_sinks(promotion_harness, monkeypatch)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert calls == {"remove": [], "complete": [], "local": [], "remote": []}
    assert lease.worktree_path.is_dir()


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
    failure_while_reserved = False
    view_calls = 0

    def fail_post_lock_view(number: int) -> PullRequest:
        nonlocal failure_while_reserved, view_calls
        view_calls += 1
        if promotion_harness.registry.get_cleanup_reservation(lease.id) is not None:
            failure_while_reserved = True
            raise ExternalServiceError("gh pr view failed: network unavailable")
        return original_view(number)

    monkeypatch.setattr(promotion_harness.github, "view_pr", fail_post_lock_view)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "error"
    assert result.exit_code == 4
    assert result.blockers[0]["code"] == "github_refresh_failed"
    assert failure_while_reserved
    assert view_calls >= 1
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


def _enable_compact_fixture(harness: Harness) -> None:
    (harness.repo / ".gitignore").write_text(
        "/node_modules/\n",
        encoding="utf-8",
    )
    git_command(harness.repo, "add", ".gitignore")
    git_command(harness.repo, "commit", "-q", "-m", "ignore dependencies")
    git_command(harness.repo, "push", "-q", "origin", "staging")


def _stale_compact_lease(
    harness: Harness,
    initiative: str,
    state: LeaseState,
) -> Lease:
    acquired = harness.acquire(initiative)
    assert acquired.lease is not None
    lease = acquired.lease
    if state is not LeaseState.ACTIVE:
        lease = harness.registry.transition(
            lease.id,
            state,
            expected_version=lease.version,
        )
    dependency = lease.worktree_path / "node_modules" / "pkg"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_bytes(b"x" * 8192)
    stale_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
        timespec="seconds"
    )
    with sqlite3.connect(harness.registry.db_path) as connection:
        connection.execute(
            """
            UPDATE worktree_leases
            SET created_at = ?, last_used_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (stale_at, stale_at, stale_at, lease.id),
        )
    current = harness.registry.get_lease(lease.id)
    assert current is not None
    return current


def test_compact_bulk_removes_ignored_dependencies_without_lifecycle_mutation(
    harness: Harness,
) -> None:
    _enable_compact_fixture(harness)
    deployable = _stale_compact_lease(
        harness,
        "blip-server",
        LeaseState.DEPLOYING,
    )
    active = _stale_compact_lease(harness, "active", LeaseState.ACTIVE)
    dirty = _stale_compact_lease(harness, "dirty", LeaseState.DIRTY)
    blocked = _stale_compact_lease(harness, "blocked", LeaseState.BLOCKED)
    before = harness.registry.get_lease(deployable.id)
    assert before is not None
    before_events = harness.registry.list_events(deployable.id)
    before_head = harness.git.head_sha(deployable.worktree_path)
    before_status = harness.git.status_porcelain(deployable.worktree_path)
    expected_usage = harness.git.compact_path_usage(
        deployable.worktree_path,
        "node_modules",
    )
    marker = harness.service._prepare_marker_path(deployable.id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"key":"prepared"}', encoding="utf-8")

    preview = harness.service.compact(
        lease_id=None,
        paths=("node_modules",),
        older_than="7d",
    )

    assert preview.decision == "preview"
    assert [action["lease_id"] for action in preview.actions] == [deployable.id]
    assert preview.actions[0]["bytes"] == expected_usage.allocated_bytes
    assert preview.actions[0]["entry_count"] == expected_usage.entry_count

    applied = harness.service.compact(
        lease_id=None,
        paths=("node_modules",),
        older_than="7d",
        apply=True,
    )

    assert applied.decision == "removed"
    assert not (deployable.worktree_path / "node_modules").exists()
    assert not marker.exists()
    assert harness.registry.get_lease(deployable.id) == before
    assert harness.registry.list_events(deployable.id) == before_events
    assert harness.git.head_sha(deployable.worktree_path) == before_head
    assert harness.git.status_porcelain(deployable.worktree_path) == before_status
    for lease in (active, dirty, blocked):
        assert (lease.worktree_path / "node_modules").exists()


def test_compact_rejects_invalid_duration_path_state_and_reservation(
    harness: Harness,
) -> None:
    _enable_compact_fixture(harness)
    active = _stale_compact_lease(harness, "active", LeaseState.ACTIVE)
    deployable = _stale_compact_lease(
        harness,
        "deployable",
        LeaseState.DEPLOYING,
    )

    invalid_duration = harness.service.compact(
        lease_id=deployable.id,
        paths=("node_modules",),
        older_than="0d",
    )
    invalid_path = harness.service.compact(
        lease_id=deployable.id,
        paths=("node_modules", "node_modules"),
        older_than="7d",
    )
    ineligible = harness.service.compact(
        lease_id=active.id,
        paths=("node_modules",),
        older_than="7d",
    )
    harness.registry.reserve_cleanup(
        deployable.id,
        expected_version=deployable.version,
        branch_sha=deployable.head_sha,
    )
    reserved = harness.service.compact(
        lease_id=deployable.id,
        paths=("node_modules",),
        older_than="7d",
    )

    assert invalid_duration.blockers[0]["code"] == "invalid_age_threshold"
    assert invalid_path.blockers[0]["code"] == "invalid_compact_path"
    assert ineligible.blockers[0]["code"] == "ineligible_state"
    assert reserved.blockers[0]["code"] == "cleanup_reserved"
    assert (deployable.worktree_path / "node_modules").exists()


@pytest.mark.parametrize(
    "paths",
    (
        (".",),
        (".git",),
        ("node_modules", "node_modules/pkg"),
        ("Node_Modules", "node_modules/pkg"),
        ("Node_Modules", "node_modules"),
        ("nested/.git/cache",),
    ),
)
def test_compact_rejects_root_git_and_overlapping_paths(
    harness: Harness,
    paths: tuple[str, ...],
) -> None:
    _enable_compact_fixture(harness)
    lease = _stale_compact_lease(harness, "invalid-paths", LeaseState.DEPLOYING)

    result = harness.service.compact(
        lease_id=lease.id,
        paths=paths,
        older_than="7d",
    )

    assert result.blockers[0]["code"] == "invalid_compact_path"
    assert (lease.worktree_path / "node_modules").exists()


def test_compact_apply_revalidates_every_candidate_before_any_removal(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_compact_fixture(harness)
    first = _stale_compact_lease(harness, "first", LeaseState.PR_OPEN)
    second = _stale_compact_lease(harness, "second", LeaseState.DEPLOYED)
    original_candidates = harness.service._compact_candidates
    calls = 0

    def dirty_before_locked_revalidation(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            (second.worktree_path / "changed.txt").write_text(
                "dirty\n",
                encoding="utf-8",
            )
        return original_candidates(*args, **kwargs)

    monkeypatch.setattr(
        harness.service,
        "_compact_candidates",
        dirty_before_locked_revalidation,
    )

    result = harness.service.compact(
        lease_id=None,
        paths=("node_modules",),
        older_than="7d",
        apply=True,
    )

    assert result.decision == "blocked"
    assert {blocker["code"] for blocker in result.blockers} == {"dirty_worktree"}
    assert (first.worktree_path / "node_modules").exists()
    assert (second.worktree_path / "node_modules").exists()


def test_compact_reports_only_completed_actions_after_remove_failure(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_compact_fixture(harness)
    first = _stale_compact_lease(harness, "remove-first", LeaseState.DEPLOYED)
    second = _stale_compact_lease(harness, "remove-second", LeaseState.DEPLOYED)
    original_remove = harness.git.remove_ignored_path
    calls = 0

    def fail_second_remove(cwd: Path, path: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GitError("injected compact removal failure")
        original_remove(cwd, path)

    monkeypatch.setattr(harness.git, "remove_ignored_path", fail_second_remove)
    result = harness.service.compact(
        lease_id=None,
        paths=("node_modules",),
        older_than="7d",
        apply=True,
    )

    assert result.blockers[0]["code"] == "compact_remove_failed"
    completed_id = result.actions[0]["lease_id"]
    assert completed_id in {first.id, second.id}
    completed = first if completed_id == first.id else second
    pending = second if completed_id == first.id else first
    assert not (completed.worktree_path / "node_modules").exists()
    assert (pending.worktree_path / "node_modules").exists()


def test_compact_apply_stops_when_repository_lock_is_unavailable(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_compact_fixture(harness)
    lease = _stale_compact_lease(harness, "locked", LeaseState.DEPLOYED)

    @contextmanager
    def unavailable_lock(*_args: object, **_kwargs: object):
        raise BlockingIOError("lock held")
        yield

    monkeypatch.setattr(service_module, "repository_lock", unavailable_lock)

    result = harness.service.compact(
        lease_id=lease.id,
        paths=("node_modules",),
        older_than="7d",
        apply=True,
    )

    assert result.decision == "blocked"
    assert result.blockers[0]["code"] == "repository_locked"
    assert (lease.worktree_path / "node_modules").exists()


def test_compact_rejects_foreign_provenance_and_symlinked_path(
    harness: Harness,
) -> None:
    _enable_compact_fixture(harness)
    lease = _stale_compact_lease(harness, "provenance", LeaseState.DEPLOYED)
    with sqlite3.connect(harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET repository_id = ? WHERE id = ?",
            ("foreign-repository", lease.id),
        )

    foreign = harness.service.compact(
        lease_id=lease.id,
        paths=("node_modules",),
        older_than="7d",
    )

    assert foreign.blockers[0]["code"] == "repository_mismatch"
    with sqlite3.connect(harness.registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET repository_id = ? WHERE id = ?",
            (harness.git.repository_id(), lease.id),
        )
    dependency = lease.worktree_path / "node_modules"
    external = harness.repo.parent / "external-node-modules"
    dependency.rename(external)
    dependency.symlink_to(external, target_is_directory=True)

    symlinked = harness.service.compact(
        lease_id=lease.id,
        paths=("node_modules",),
        older_than="7d",
    )

    assert symlinked.blockers[0]["code"] == "unsafe_compact_path"
    assert external.exists()


def _add_main_only_change(
    harness: PromotionHarness,
    *,
    path: str = "production.txt",
    content: str = "production\n",
) -> str:
    git_command(harness.repo, "checkout", "-q", "main")
    changed_path = harness.repo / path
    changed_path.parent.mkdir(parents=True, exist_ok=True)
    changed_path.write_text(content, encoding="utf-8")
    git_command(harness.repo, "add", path)
    git_command(harness.repo, "commit", "-q", "-m", "production-only change")
    source_sha = git_command(harness.repo, "rev-parse", "HEAD")
    git_command(harness.repo, "push", "-q", "origin", "main")
    git_command(harness.repo, "checkout", "-q", "staging")
    return source_sha


def _prepare_sync_three_way_noop(harness: PromotionHarness) -> tuple[str, str]:
    baseline = "base\nline-2\nline-3\nline-4\nline-5\nline-6\nline-7\ntail\n"
    source_content = baseline.replace("base\n", "source\n", 1)
    git_command(harness.repo, "checkout", "-q", "staging")
    (harness.repo / "README.txt").write_text(baseline, encoding="utf-8")
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "shared readme baseline")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    git_command(harness.repo, "checkout", "-q", "main")
    git_command(harness.repo, "merge", "--ff-only", "-q", "staging")
    git_command(harness.repo, "push", "-q", "origin", "main")
    (harness.repo / "README.txt").write_text(source_content, encoding="utf-8")
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "source readme update")
    source_sha = git_command(harness.repo, "rev-parse", "HEAD")
    git_command(harness.repo, "push", "-q", "origin", "main")
    git_command(harness.repo, "checkout", "-q", "staging")
    (harness.repo / "README.txt").write_text(
        source_content.replace("tail\n", "staging tail\n"), encoding="utf-8"
    )
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "staging readme extension")
    target_sha = git_command(harness.repo, "rev-parse", "HEAD")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    return source_sha, target_sha


def test_sync_preview_and_apply_preserve_staging_only_changes(
    promotion_harness: PromotionHarness,
) -> None:
    source_sha = _add_main_only_change(promotion_harness)
    target_sha = git_command(
        promotion_harness.repo, "rev-parse", "refs/remotes/origin/staging"
    )

    preview = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=False,
    )

    assert preview.decision == "preview"
    assert preview.actions[0]["source_head_sha"] == source_sha
    assert preview.actions[0]["target_base_sha"] == target_sha
    assert preview.actions[0]["changed_paths"] == ["production.txt"]
    assert promotion_harness.registry.list_leases() == []
    assert promotion_harness.github.create_calls == []

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    lease = result.lease
    assert lease.purpose is Purpose.FEATURE
    assert lease.state is LeaseState.PR_OPEN
    assert lease.deployment_state is DeploymentState.NOT_REQUIRED
    assert lease.source_head_sha == source_sha
    assert lease.target_base_sha == target_sha
    assert lease.reviewed_paths == ("production.txt",)
    assert (lease.worktree_path / "production.txt").read_text(encoding="utf-8") == (
        "production\n"
    )
    assert (lease.worktree_path / "team.txt").read_text(encoding="utf-8") == "team\n"
    assert (lease.worktree_path / "feature.txt").read_text(encoding="utf-8") == (
        "feature\n"
    )
    assert promotion_harness.git.changed_paths(
        lease.worktree_path,
        target_sha,
        lease.head_sha,
        find_renames=False,
    ) == ("production.txt",)
    assert len(promotion_harness.github.create_calls) == 1
    body = promotion_harness.github.create_calls[0]["body"]
    assert body.splitlines() == [
        "AWF-Sync-From: main",
        "AWF-Sync-To: staging",
        f"AWF-Sync-Base: {lease.source_base_sha}",
        f"AWF-Sync-Head: {source_sha}",
        f"AWF-Sync-Target: {target_sha}",
        f"AWF-Lease: {lease.id}",
        "AWF-No-Promote: true",
    ]
    assert promotion_harness.git.commit_message(lease.worktree_path).endswith(body)


def test_sync_uses_conventional_commit_subject(
    promotion_harness: PromotionHarness,
) -> None:
    _add_main_only_change(promotion_harness)

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert (
        promotion_harness.git.commit_message(result.lease.worktree_path).splitlines()[
            0
        ]
        == "chore(sync): sync main to staging"
    )


def test_sync_is_noop_when_staging_contains_main(
    promotion_harness: PromotionHarness,
) -> None:
    preview = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=False,
    )
    applied = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert preview.decision == "noop"
    assert applied.decision == "noop"
    assert preview.actions[0]["kind"] == "no_sync_required"
    assert promotion_harness.registry.list_leases() == []
    assert promotion_harness.github.create_calls == []


def test_sync_cleans_up_when_source_patch_has_no_indexed_target_delta(
    promotion_harness: PromotionHarness,
) -> None:
    source_sha, target_sha = _prepare_sync_three_way_noop(promotion_harness)

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "noop"
    assert result.lease is None
    assert [action["kind"] for action in result.actions] == [
        "no_sync_required",
        "remove_worktree",
        "delete_local_branch",
    ]
    assert promotion_harness.registry.list_leases(include_removed=False) == []
    leases = promotion_harness.registry.list_leases()
    assert len(leases) == 1
    lease = leases[0]
    assert lease.state is LeaseState.REMOVED
    assert lease.source_head_sha == source_sha
    assert lease.target_base_sha == target_sha
    assert not lease.worktree_path.exists()
    assert promotion_harness.git.remote_branch_sha(lease.branch) is None
    with pytest.raises(GitError):
        promotion_harness.git.resolve_ref(lease.branch)
    assert promotion_harness.github.create_calls == []


def _blocked_sync_apply_failed_noop_lease(
    harness: PromotionHarness, monkeypatch: pytest.MonkeyPatch
) -> Lease:
    _, target_sha = _prepare_sync_three_way_noop(harness)
    original_index_tree_sha = harness.git.index_tree_sha
    calls = 0

    def force_nonempty_index_tree(cwd: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "0" * 40
        return original_index_tree_sha(cwd)

    monkeypatch.setattr(
        harness.git, "index_tree_sha", force_nonempty_index_tree
    )
    first = harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )
    monkeypatch.setattr(harness.git, "index_tree_sha", original_index_tree_sha)

    assert first.decision == "blocked"
    assert first.blockers[0]["code"] == "sync_apply_failed"
    assert first.lease is not None
    blocked = harness.registry.get_lease(first.lease.id)
    assert blocked is not None
    assert blocked.state is LeaseState.BLOCKED
    assert blocked.head_sha == target_sha
    assert harness.git.head_sha(blocked.worktree_path) == target_sha
    assert harness.github.create_calls == []
    return blocked


def test_sync_recovers_clean_apply_failure_as_noop(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = _blocked_sync_apply_failed_noop_lease(
        promotion_harness, monkeypatch
    )

    recovered = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert recovered.decision == "noop"
    assert recovered.lease is None
    assert [action["kind"] for action in recovered.actions] == [
        "no_sync_required",
        "remove_worktree",
        "delete_local_branch",
    ]
    persisted = promotion_harness.registry.get_lease(blocked.id)
    assert persisted is not None
    assert persisted.state is LeaseState.REMOVED
    assert not blocked.worktree_path.exists()
    assert promotion_harness.git.remote_branch_sha(blocked.branch) is None
    assert promotion_harness.github.create_calls == []


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    (("dirty", "dirty_sync_lease"), ("head", "sync_lease_head_mismatch")),
)
def test_sync_noop_recovery_refuses_dirty_or_head_drift(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    blocker: str,
) -> None:
    blocked = _blocked_sync_apply_failed_noop_lease(
        promotion_harness, monkeypatch
    )
    if mutation == "dirty":
        (blocked.worktree_path / "untracked.txt").write_text(
            "unexpected\n", encoding="utf-8"
        )
    else:
        (blocked.worktree_path / "README.txt").write_text(
            "tampered\n", encoding="utf-8"
        )
        git_command(blocked.worktree_path, "add", "README.txt")
        git_command(
            blocked.worktree_path, "commit", "-q", "-m", "tamper sync worktree"
        )

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "blocked"
    assert result.blockers[0]["code"] == blocker
    persisted = promotion_harness.registry.get_lease(blocked.id)
    assert persisted is not None
    assert persisted.state is LeaseState.BLOCKED
    assert blocked.worktree_path.exists()
    assert promotion_harness.github.create_calls == []


def test_sync_resumes_verified_commit_after_transient_verify_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_main_only_change(promotion_harness)
    original_verify = promotion_harness.service._verify_promotion
    attempts = 0

    def fail_once(worktree_path: Path) -> tuple[dict[str, object], ...]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary production verification failure")
        return original_verify(worktree_path)

    monkeypatch.setattr(promotion_harness.service, "_verify_promotion", fail_once)
    first = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert first.decision == "blocked"
    assert first.blockers[0]["code"] == "sync_apply_failed"
    assert first.lease is not None
    assert first.lease.target_base_sha is not None
    assert (
        promotion_harness.git.head_sha(first.lease.worktree_path)
        != first.lease.target_base_sha
    )

    resumed = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.state is LeaseState.PR_OPEN
    assert attempts == 2


def test_sync_resumes_legacy_commit_subject(
    promotion_harness: PromotionHarness,
) -> None:
    _add_main_only_change(promotion_harness)
    promotion_harness.github.create_error = ExternalServiceError("temporary outage")
    first = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert first.exit_code == 4
    assert first.lease is not None
    lease = first.lease
    assert lease.source_base_sha is not None
    assert lease.source_head_sha is not None
    assert lease.target_base_sha is not None
    legacy_message = promotion_harness.service._legacy_sync_message(
        source_branch="main",
        target_branch="staging",
        merge_base=lease.source_base_sha,
        source_sha=lease.source_head_sha,
        target_sha=lease.target_base_sha,
        lease=lease,
    )
    git_command(
        lease.worktree_path, "commit", "--amend", "-q", "-m", legacy_message
    )
    git_command(
        lease.worktree_path,
        "push",
        "-q",
        "--force",
        "origin",
        f"HEAD:refs/heads/{lease.branch}",
    )
    promotion_harness.github.create_error = None

    resumed = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.state is LeaseState.PR_OPEN


@pytest.mark.parametrize(
    ("published", "blocker"),
    (("branch", "sync_branch_exists"), ("pull_request", "sync_pr_open")),
)
def test_sync_noop_recovery_refuses_published_branch_or_pull_request(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    published: str,
    blocker: str,
) -> None:
    blocked = _blocked_sync_apply_failed_noop_lease(
        promotion_harness, monkeypatch
    )
    assert blocked.target_base_sha is not None
    if published == "branch":
        git_command(
            promotion_harness.repo,
            "push",
            "-q",
            "origin",
            f"{blocked.branch}:refs/heads/{blocked.branch}",
        )
    else:
        promotion_harness.github.open_prs[(blocked.branch, "staging")] = replace(
            pull_request(
                number=901, state="OPEN", head_sha=blocked.target_base_sha
            ),
            base_ref="staging",
            base_sha=blocked.target_base_sha,
            head_ref=blocked.branch,
        )

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "blocked"
    assert result.blockers[0]["code"] == blocker
    assert blocked.worktree_path.exists()


def test_sync_noop_retries_after_cleanup_reservation_is_released(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_sync_three_way_noop(promotion_harness)
    original_remove = promotion_harness.git.remove_worktree

    def fail_remove(_path: Path) -> None:
        raise GitError("temporary worktree removal failure")

    monkeypatch.setattr(promotion_harness.git, "remove_worktree", fail_remove)
    first = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )
    monkeypatch.setattr(promotion_harness.git, "remove_worktree", original_remove)

    assert first.decision == "blocked"
    assert first.blockers[0]["code"] == "worktree_remove_failed"
    assert first.lease is not None
    assert promotion_harness.registry.list_events(first.lease.id)[
        -1
    ].event_type == "cleanup_released"

    retried = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert retried.decision == "noop"
    persisted = promotion_harness.registry.get_lease(first.lease.id)
    assert persisted is not None
    assert persisted.state is LeaseState.REMOVED


def test_sync_noop_retries_after_external_preflight_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sha, _ = _prepare_sync_three_way_noop(promotion_harness)
    branch = promotion_harness.service._sync_branch(
        promotion_harness.service._sync_initiative("main", "staging"), source_sha
    )
    original_remote_branch_sha = promotion_harness.git.remote_branch_sha
    branch_lookups = 0

    def fail_final_noop_preflight(branch_name: str) -> str | None:
        nonlocal branch_lookups
        if branch_name == branch:
            branch_lookups += 1
            if branch_lookups == 2:
                raise GitRemoteError(
                    "temporary synchronization branch lookup failure"
                )
        return original_remote_branch_sha(branch_name)

    monkeypatch.setattr(
        promotion_harness.git, "remote_branch_sha", fail_final_noop_preflight
    )
    first = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )
    monkeypatch.setattr(
        promotion_harness.git, "remote_branch_sha", original_remote_branch_sha
    )

    assert first.exit_code == 4
    assert first.lease is not None
    assert first.lease.state is LeaseState.ACTIVE

    retried = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert retried.decision == "noop"


def test_sync_noop_completes_cleanup_after_partial_worktree_removal(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_sync_three_way_noop(promotion_harness)
    original_remove = promotion_harness.git.remove_worktree

    def remove_then_report_failure(path: Path) -> None:
        original_remove(path)
        raise GitError("worktree removed after transport failure")

    monkeypatch.setattr(
        promotion_harness.git, "remove_worktree", remove_then_report_failure
    )
    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "noop"
    removed = promotion_harness.registry.list_leases()[0]
    assert removed.state is LeaseState.REMOVED
    assert not removed.worktree_path.exists()
    with pytest.raises(GitError):
        promotion_harness.git.resolve_ref(removed.branch)


def test_sync_preserves_source_file_mode_changes(
    promotion_harness: PromotionHarness,
) -> None:
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    git_command(
        promotion_harness.repo,
        "update-index",
        "--chmod=+x",
        "README.txt",
    )
    (promotion_harness.repo / "README.txt").chmod(0o755)
    git_command(promotion_harness.repo, "commit", "-q", "-m", "make readme executable")
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.reviewed_paths == ("README.txt",)
    assert promotion_harness.git.path_entry(
        result.lease.head_sha, "README.txt"
    ) == promotion_harness.git.path_entry(
        "refs/remotes/origin/main", "README.txt"
    )


def test_sync_preserves_conflicted_worktree_without_opening_pr(
    promotion_harness: PromotionHarness,
) -> None:
    (promotion_harness.repo / "README.txt").write_text(
        "staging version\n", encoding="utf-8"
    )
    git_command(promotion_harness.repo, "add", "README.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "staging readme")
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    _add_main_only_change(
        promotion_harness,
        path="README.txt",
        content="production version\n",
    )

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "blocked"
    assert result.blockers[0]["code"] == "sync_target_conflict"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert result.lease.worktree_path.exists()
    assert promotion_harness.github.create_calls == []


def test_sync_pr_cannot_be_added_to_promotion_or_release(
    promotion_harness: PromotionHarness,
) -> None:
    promotion_harness.github.prs[372] = replace(
        promotion_harness.github.prs[372],
        body="AWF-Sync-From: main\nAWF-No-Promote: true",
    )

    promotion = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )
    opened = promotion_harness.service.release_open(
        release_id="no-sync-loop",
        target_branch="main",
        apply=True,
    )
    release = promotion_harness.service.release_add(
        release_id="no-sync-loop",
        source_pr=372,
        apply=False,
    )

    assert promotion.blockers[0]["code"] == "source_pr_not_promotable"
    assert opened.decision == "ready"
    assert release.blockers[0]["code"] == "source_pr_not_promotable"


@pytest.mark.parametrize(
    ("drifted_branch", "blocker_code"),
    (("main", "sync_source_changed"), ("staging", "sync_target_changed")),
)
def test_sync_blocks_remote_drift_before_publish(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    drifted_branch: str,
    blocker_code: str,
) -> None:
    _add_main_only_change(promotion_harness)
    remote_branch_sha = promotion_harness.git.remote_branch_sha

    def drifted_remote_sha(branch: str) -> str | None:
        if branch == drifted_branch:
            return "f" * 40
        return remote_branch_sha(branch)
    monkeypatch.setattr(
        promotion_harness.git,
        "remote_branch_sha",
        drifted_remote_sha,
    )

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "blocked"
    assert result.blockers[0]["code"] == blocker_code
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert promotion_harness.github.create_calls == []



def test_sync_allows_clean_three_way_merge_with_staging_changes(
    promotion_harness: PromotionHarness,
) -> None:
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "shared.txt").write_text(
        "production=old\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nstaging=old\n",
    )
    git_command(promotion_harness.repo, "add", "shared.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "shared baseline")
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")
    git_command(
        promotion_harness.repo,
        "merge",
        "--no-ff",
        "-q",
        "main",
        "-m",
        "merge shared baseline",
    )
    (promotion_harness.repo / "shared.txt").write_text(
        "production=old\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nstaging=new\n",
    )
    git_command(promotion_harness.repo, "add", "shared.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "staging shared change")
    git_command(promotion_harness.repo, "push", "-q", "origin", "staging")
    git_command(promotion_harness.repo, "checkout", "-q", "main")
    (promotion_harness.repo / "shared.txt").write_text(
        "production=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nstaging=old\n",
    )
    git_command(promotion_harness.repo, "add", "shared.txt")
    git_command(promotion_harness.repo, "commit", "-q", "-m", "production shared change")
    git_command(promotion_harness.repo, "push", "-q", "origin", "main")
    git_command(promotion_harness.repo, "checkout", "-q", "staging")

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "ready", result.to_dict()
    assert result.lease is not None
    assert (result.lease.worktree_path / "shared.txt").read_text(
        encoding="utf-8"
    ) == (
        "production=new\nstable-1\nstable-2\nstable-3\nstable-4\nstable-5\nstaging=new\n"
    )


def test_sync_refuses_unowned_remote_branch(
    promotion_harness: PromotionHarness,
) -> None:
    source_sha = _add_main_only_change(promotion_harness)
    initiative = promotion_harness.service._sync_initiative("main", "staging")
    branch = promotion_harness.service._sync_branch(initiative, source_sha)
    staging_sha = promotion_harness.git.resolve_ref("refs/remotes/origin/staging")
    git_command(
        promotion_harness.repo,
        "push",
        "-q",
        "origin",
        f"staging:refs/heads/{branch}",
    )

    result = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.decision == "blocked"
    assert result.blockers[0]["code"] == "sync_branch_exists"
    assert promotion_harness.git.remote_branch_sha(branch) == staging_sha
    assert promotion_harness.registry.list_leases() == []


def test_sync_resumes_publication_after_github_failure(
    promotion_harness: PromotionHarness,
) -> None:
    _add_main_only_change(promotion_harness)
    promotion_harness.github.create_error = ExternalServiceError("temporary outage")

    first = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert first.exit_code == 4
    assert first.lease is not None
    persisted = promotion_harness.registry.get_lease(first.lease.id)
    assert persisted is not None
    assert persisted.state is LeaseState.ACTIVE
    promotion_harness.github.create_error = None

    resumed = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.id == first.lease.id
    assert resumed.lease.state is LeaseState.PR_OPEN


def test_sync_resume_rejects_amended_commit_with_extra_path(
    promotion_harness: PromotionHarness,
) -> None:
    _add_main_only_change(promotion_harness)
    promotion_harness.github.create_error = ExternalServiceError("temporary outage")
    first = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )
    assert first.lease is not None
    worktree_path = first.lease.worktree_path
    (worktree_path / "extra.txt").write_text("unexpected\n", encoding="utf-8")
    git_command(worktree_path, "add", "extra.txt")
    git_command(worktree_path, "commit", "-q", "--amend", "--no-edit")
    promotion_harness.github.create_error = None

    resumed = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert resumed.decision == "blocked"
    assert resumed.blockers[0]["code"] == "sync_delta_mismatch"


def test_sync_branch_identity_remains_non_promotable_after_body_edit(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.github.prs[372]
    promotion_harness.github.prs[372] = replace(
        source,
        head_ref="awf/sync-deadbeefdeadbeef-0123456789ab/feature",
        body="marker removed",
    )

    result = promotion_harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=False,
    )

    assert result.blockers[0]["code"] == "source_pr_not_promotable"


def _blocked_sync_target_conflict(harness: PromotionHarness) -> Lease:
    (harness.repo / "README.txt").write_text(
        "staging version\n", encoding="utf-8"
    )
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "staging readme")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    _add_main_only_change(
        harness,
        path="README.txt",
        content="production version\n",
    )
    result = harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "sync_target_conflict"
    assert result.lease is not None
    assert result.lease.conflicted_paths == ("README.txt",)
    return result.lease

def _blocked_sync_target_conflict_with_source_deleted_clean_path(
    harness: PromotionHarness,
) -> tuple[Lease, tuple[str, ...]]:
    clean_paths = (
        "src/vote/added.py",
        "src/vote/removed.py",
        "src/vote/tally.py",
    )
    shared_path = harness.repo / clean_paths[1]
    git_command(harness.repo, "checkout", "-q", "staging")
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text("shared vote\n", encoding="utf-8")
    git_command(harness.repo, "add", clean_paths[1])
    git_command(harness.repo, "commit", "-q", "-m", "shared vote baseline")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    git_command(harness.repo, "checkout", "-q", "main")
    git_command(harness.repo, "merge", "--no-edit", "-q", "staging")
    git_command(harness.repo, "push", "-q", "origin", "main")
    git_command(harness.repo, "checkout", "-q", "staging")
    (harness.repo / "README.txt").write_text(
        "staging version\n", encoding="utf-8"
    )
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "staging readme")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    git_command(harness.repo, "checkout", "-q", "main")
    shared_path.unlink()
    (harness.repo / clean_paths[0]).write_text(
        "source addition\n", encoding="utf-8"
    )
    (harness.repo / clean_paths[2]).write_text(
        "source tally\n", encoding="utf-8"
    )
    (harness.repo / "README.txt").write_text(
        "production version\n", encoding="utf-8"
    )
    git_command(harness.repo, "add", "-A")
    git_command(harness.repo, "commit", "-q", "-m", "source vote changes")
    git_command(harness.repo, "push", "-q", "origin", "main")
    git_command(harness.repo, "checkout", "-q", "staging")
    result = harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "sync_target_conflict"
    assert result.lease is not None
    assert result.lease.conflicted_paths == ("README.txt",)
    assert result.lease.reviewed_paths == tuple(
        sorted(("README.txt", *clean_paths))
    )
    return result.lease, clean_paths


def _blocked_sync_target_conflict_with_clean_source_paths(
    harness: PromotionHarness,
) -> tuple[Lease, tuple[str, ...]]:
    clean_paths = (
        "src/vote/added.py",
        "src/vote/ballot.py",
        "src/vote/tally.py",
    )
    git_command(harness.repo, "checkout", "-q", "staging")
    for path, content in (
        (clean_paths[1], "staging ballot\n"),
        (clean_paths[2], "staging tally\n"),
    ):
        changed_path = harness.repo / path
        changed_path.parent.mkdir(parents=True, exist_ok=True)
        changed_path.write_text(content, encoding="utf-8")
    git_command(harness.repo, "add", clean_paths[1], clean_paths[2])
    git_command(harness.repo, "commit", "-q", "-m", "shared vote baseline")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    git_command(harness.repo, "checkout", "-q", "main")
    git_command(harness.repo, "merge", "--no-edit", "-q", "staging")
    git_command(harness.repo, "push", "-q", "origin", "main")
    git_command(harness.repo, "checkout", "-q", "staging")
    (harness.repo / "README.txt").write_text(
        "staging version\n", encoding="utf-8"
    )
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "staging readme")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    git_command(harness.repo, "checkout", "-q", "main")
    (harness.repo / clean_paths[0]).write_text(
        "source addition\n", encoding="utf-8"
    )
    (harness.repo / clean_paths[1]).write_text(
        "source ballot\n", encoding="utf-8"
    )
    (harness.repo / clean_paths[2]).write_text(
        "source tally\n", encoding="utf-8"
    )
    (harness.repo / "README.txt").write_text(
        "production version\n", encoding="utf-8"
    )
    git_command(harness.repo, "add", "-A")
    git_command(harness.repo, "commit", "-q", "-m", "source vote changes")
    git_command(harness.repo, "push", "-q", "origin", "main")
    git_command(harness.repo, "checkout", "-q", "staging")
    result = harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "sync_target_conflict"
    assert result.lease is not None
    assert result.lease.conflicted_paths == ("README.txt",)
    assert result.lease.reviewed_paths == tuple(
        sorted(("README.txt", *clean_paths))
    )
    return result.lease, clean_paths


def test_discard_sync_previews_and_removes_only_stale_conflict_cleanup(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")
    before = promotion_harness.registry.get_lease(lease.id)
    assert before is not None
    calls: list[tuple[Path, bool]] = []
    original_remove = promotion_harness.git.remove_worktree

    def record_remove(path: Path, *, force: bool = False) -> None:
        calls.append((path, force))
        original_remove(path, force=force)

    monkeypatch.setattr(promotion_harness.git, "remove_worktree", record_remove)

    preview = promotion_harness.service.discard_sync(lease.id)

    assert preview.decision == "preview"
    assert [action["kind"] for action in preview.actions] == [
        "remove_worktree",
        "delete_local_branch",
    ]
    assert promotion_harness.registry.get_lease(lease.id) == before

    removed = promotion_harness.service.discard_sync(lease.id, apply=True)

    assert removed.decision == "removed"
    assert calls == [(lease.worktree_path, False)]
    assert not lease.worktree_path.exists()
    stored = promotion_harness.registry.get_lease(lease.id)
    assert stored is not None
    assert stored.state is LeaseState.REMOVED
    with pytest.raises(GitError):
        promotion_harness.git.resolve_ref(lease.branch)



def test_discard_sync_restores_conflict_after_late_untracked_removal_failure(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")
    original_restore = promotion_harness.git.restore_paths_to_ref

    def restore_then_create_untracked(
        cwd: Path, ref: str, paths: tuple[str, ...]
    ) -> None:
        original_restore(cwd, ref, paths)
        (cwd / "late-operator.txt").write_text("preserve me\n", encoding="utf-8")

    monkeypatch.setattr(
        promotion_harness.git, "restore_paths_to_ref", restore_then_create_untracked
    )

    result = promotion_harness.service.discard_sync(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "worktree_remove_failed"
    assert (lease.worktree_path / "late-operator.txt").read_text(
        encoding="utf-8"
    ) == "preserve me\n"
    stored = promotion_harness.registry.get_lease(lease.id)
    assert stored is not None
    assert stored.state is LeaseState.BLOCKED
    assert stored.conflicted_paths == lease.conflicted_paths
    assert promotion_harness.git.unmerged_paths(lease.worktree_path) == (
        "README.txt",
    )
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None
    monkeypatch.setattr(
        promotion_harness.git, "restore_paths_to_ref", original_restore
    )
    (lease.worktree_path / "late-operator.txt").unlink()

    retried = promotion_harness.service.discard_sync(lease.id, apply=True)

    assert retried.decision == "removed"


def test_discard_sync_retains_reservation_when_conflict_rebuild_fails(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")
    original_restore = promotion_harness.git.restore_paths_to_ref
    applied_patches: list[bytes] = []

    def restore_then_create_untracked(
        cwd: Path, ref: str, paths: tuple[str, ...]
    ) -> None:
        original_restore(cwd, ref, paths)
        (cwd / "late-operator.txt").write_text("preserve me\n", encoding="utf-8")

    def apply_without_conflict(cwd: Path, patch: bytes) -> None:
        applied_patches.append(patch)

    monkeypatch.setattr(
        promotion_harness.git, "restore_paths_to_ref", restore_then_create_untracked
    )
    monkeypatch.setattr(
        promotion_harness.git, "apply_indexed_patch", apply_without_conflict
    )

    result = promotion_harness.service.discard_sync(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "cleanup_reserved"
    assert applied_patches
    assert (lease.worktree_path / "late-operator.txt").read_text(
        encoding="utf-8"
    ) == "preserve me\n"
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is not None


@pytest.mark.parametrize("replacement", ("directory", "symlink"))
def test_discard_sync_retains_reservation_when_worktree_root_changes(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")
    original_restore = promotion_harness.git.restore_paths_to_ref
    replacement_root = promotion_harness.repo.parent / "replacement-root"
    replacement_root.mkdir()
    sentinel = replacement_root / "do-not-overwrite.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    def restore_then_replace_root(
        cwd: Path, ref: str, paths: tuple[str, ...]
    ) -> None:
        original_restore(cwd, ref, paths)
        parked = cwd.with_name(f"{cwd.name}-parked")
        cwd.rename(parked)
        if replacement == "symlink":
            cwd.symlink_to(replacement_root, target_is_directory=True)
        else:
            cwd.mkdir()
            (cwd / "do-not-overwrite.txt").write_text("keep\n", encoding="utf-8")

    monkeypatch.setattr(
        promotion_harness.git, "restore_paths_to_ref", restore_then_replace_root
    )

    def remove_worktree_if_called(path: Path, *, force: bool = False) -> None:
        raise AssertionError(f"unexpected removal of {path}")

    monkeypatch.setattr(
        promotion_harness.git, "remove_worktree", remove_worktree_if_called
    )

    result = promotion_harness.service.discard_sync(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "cleanup_reserved"
    protected_root = (
        replacement_root if replacement == "symlink" else lease.worktree_path
    )
    assert (protected_root / "do-not-overwrite.txt").read_text(
        encoding="utf-8"
    ) == "keep\n"
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is not None


def test_discard_sync_rejects_current_conflict_and_untracked_changes(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)

    current = promotion_harness.service.discard_sync(lease.id)

    assert current.status == "blocked"
    assert current.blockers[0]["code"] == "sync_lease_not_stale"
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")
    (lease.worktree_path / "operator.txt").write_text(
        "operator\n", encoding="utf-8"
    )

    scoped = promotion_harness.service.discard_sync(lease.id)

    assert scoped.status == "blocked"
    assert scoped.blockers[0]["code"] == "sync_resolution_scope_mismatch"
    assert lease.worktree_path.exists()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("remote_branch", "remote_branch_present"),
        ("event", "sync_conflict_not_recorded"),
    ],
)
def test_discard_sync_rejects_published_or_non_conflict_history(
    promotion_harness: PromotionHarness,
    mutation: str,
    code: str,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")
    if mutation == "remote_branch":
        git_command(
            lease.worktree_path, "push", "-q", "-u", "origin", lease.branch
        )
    else:
        promotion_harness.registry.record_cleanup_event(
            lease.id,
            event_type="sync_publish_failed",
            summary="publication was attempted",
        )

    result = promotion_harness.service.discard_sync(lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == code
    assert lease.worktree_path.exists()


@pytest.mark.parametrize("operation", ("discard_sync", "recover_sync"))
def test_sync_discard_and_recovery_reject_all_state_pull_requests(
    promotion_harness: PromotionHarness,
    operation: str,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    promotion_harness.github.prs[901] = replace(
        pull_request(number=901, state="MERGED", head_sha=lease.head_sha),
        head_ref=lease.branch,
    )
    if operation == "discard_sync":
        _add_main_only_change(promotion_harness, path="later.txt", content="later\n")

    result = getattr(promotion_harness.service, operation)(lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "sync_pr_present"
    assert lease.worktree_path.exists()


def test_discard_sync_recovers_interrupted_reservation_after_removal(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")
    reservation = promotion_harness.registry.reserve_cleanup(
        lease.id,
        expected_version=lease.version,
        branch_sha=lease.head_sha,
    )
    promotion_harness.git.remove_worktree(lease.worktree_path, force=True)

    result = promotion_harness.service.discard_sync(lease.id, apply=True)

    assert result.decision == "removed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.REMOVED
    assert promotion_harness.registry.get_cleanup_reservation(lease.id) is None
    assert reservation.branch_sha == lease.target_base_sha


def test_recover_sync_stages_recorded_conflicts_and_creates_merge_commit(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    (lease.worktree_path / "README.txt").write_text(
        "resolved synchronization\n", encoding="utf-8"
    )

    preview = promotion_harness.service.recover_sync(lease.id)

    assert preview.decision == "preview"
    assert [action["kind"] for action in preview.actions] == [
        "resolve_sync_target_conflict",
        "stage_paths",
        "commit_sync_resolution",
        "verify_production",
        "push_branch",
        "open_pull_request",
    ]

    recovered = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert recovered.decision == "ready"
    assert recovered.lease is not None
    assert recovered.lease.state is LeaseState.PR_OPEN
    assert promotion_harness.git.commit_parents(recovered.lease.head_sha) == (
        lease.target_base_sha,
        lease.source_head_sha,
    )
    assert (
        promotion_harness.git.changed_path_endpoints(
            recovered.lease.worktree_path,
            lease.target_base_sha,
            recovered.lease.head_sha,
        )
        == ("README.txt",)
    )


@pytest.mark.parametrize(
    "source_entries_retained",
    (True, False),
    ids=("clean_source_entries_retained", "clean_source_entries_missing"),
)
def test_recover_sync_materializes_nonconflicted_source_paths(
    promotion_harness: PromotionHarness,
    source_entries_retained: bool,
) -> None:
    lease, clean_paths = _blocked_sync_target_conflict_with_clean_source_paths(
        promotion_harness
    )
    assert lease.source_head_sha is not None
    assert lease.target_base_sha is not None
    source_entries = {
        path: promotion_harness.git.path_entry(lease.source_head_sha, path)
        for path in clean_paths
    }
    target_entries = {
        path: promotion_harness.git.path_entry(lease.target_base_sha, path)
        for path in clean_paths
    }
    assert all(source_entries[path] is not None for path in clean_paths)
    assert target_entries[clean_paths[0]] is None
    assert target_entries[clean_paths[1]] is not None
    assert target_entries[clean_paths[2]] is not None
    if source_entries_retained:
        promotion_harness.git.restore_paths_to_ref(
            lease.worktree_path, lease.source_head_sha, clean_paths
        )
    else:
        promotion_harness.git.restore_paths_to_ref(
            lease.worktree_path, lease.target_base_sha, clean_paths[1:]
        )
        added_path = lease.worktree_path / clean_paths[0]
        git_command(
            lease.worktree_path, "update-index", "--force-remove", clean_paths[0]
        )
        if added_path.exists():
            added_path.unlink()
    assert dict(
        promotion_harness.git.index_entry_snapshot(
            lease.worktree_path, clean_paths
        )
    ) == (source_entries if source_entries_retained else target_entries)
    (lease.worktree_path / "README.txt").write_text(
        "resolved synchronization\n", encoding="utf-8"
    )

    recovered = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert recovered.decision == "ready"
    assert recovered.lease is not None
    assert (
        promotion_harness.git.changed_path_endpoints(
            recovered.lease.worktree_path,
            lease.target_base_sha,
            recovered.lease.head_sha,
        )
        == lease.reviewed_paths
    )
    for path, source_entry in source_entries.items():
        assert (
            promotion_harness.git.path_entry(recovered.lease.head_sha, path)
            == source_entry
        )


def test_recover_sync_rematerializes_clean_source_paths_after_rejection(
    promotion_harness: PromotionHarness,
) -> None:
    lease, clean_paths = _blocked_sync_target_conflict_with_clean_source_paths(
        promotion_harness
    )
    assert lease.source_head_sha is not None
    assert lease.target_base_sha is not None
    source_entries = {
        path: promotion_harness.git.path_entry(lease.source_head_sha, path)
        for path in clean_paths
    }
    promotion_harness.git.restore_paths_to_ref(
        lease.worktree_path, lease.target_base_sha, clean_paths[1:]
    )
    added_path = lease.worktree_path / clean_paths[0]
    git_command(
        lease.worktree_path, "update-index", "--force-remove", clean_paths[0]
    )
    if added_path.exists():
        added_path.unlink()
    resolved = lease.worktree_path / "README.txt"
    resolved.write_text(
        "<<<<<<< ours\nstaging version\n=======\nproduction version\n>>>>>>> theirs\n",
        encoding="utf-8",
    )

    blocked = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert blocked.status == "blocked"
    assert blocked.blockers[0]["code"] == "sync_conflict_markers_present"
    assert promotion_harness.git.unmerged_paths(lease.worktree_path) == (
        "README.txt",
    )
    assert dict(
        promotion_harness.git.index_entry_snapshot(
            lease.worktree_path, clean_paths
        )
    ) == source_entries
    status_entries = {
        entry.path: entry
        for entry in promotion_harness.git.status_porcelain_entries(
            lease.worktree_path
        )
    }
    assert all(
        status_entries[path].worktree_status == " " for path in clean_paths
    )
    resolved.write_text("resolved synchronization\n", encoding="utf-8")
    preview = promotion_harness.service.recover_sync(lease.id)

    assert preview.decision == "preview"

    recovered = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert recovered.decision == "ready"


def test_recover_sync_rejects_source_deleted_clean_path_without_mutation(
    promotion_harness: PromotionHarness,
) -> None:
    lease, clean_paths = (
        _blocked_sync_target_conflict_with_source_deleted_clean_path(
            promotion_harness
        )
    )
    assert lease.source_head_sha is not None
    assert lease.target_base_sha is not None
    deleted_path = clean_paths[1]
    assert (
        promotion_harness.git.path_entry(lease.source_head_sha, deleted_path)
        is None
    )
    promotion_harness.git.restore_paths_to_ref(
        lease.worktree_path, lease.target_base_sha, (deleted_path,)
    )
    before_index = promotion_harness.git.index_entry_snapshot(
        lease.worktree_path, (deleted_path,)
    )
    before_content = (lease.worktree_path / deleted_path).read_text(
        encoding="utf-8"
    )

    blocked = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert blocked.status == "blocked"
    assert blocked.blockers[0]["code"] == "sync_recovery_failed"
    assert (
        promotion_harness.git.index_entry_snapshot(
            lease.worktree_path, (deleted_path,)
        )
        == before_index
    )
    assert (lease.worktree_path / deleted_path).read_text(
        encoding="utf-8"
    ) == before_content


def test_recover_sync_restores_conflict_index_after_marker_and_delta_rejections(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    resolved = lease.worktree_path / "README.txt"
    resolved.write_text(
        "<<<<<<< ours\nstaging version\n=======\nproduction version\n>>>>>>> theirs\n",
        encoding="utf-8",
    )

    markers = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert markers.status == "blocked"
    assert markers.blockers[0]["code"] == "sync_conflict_markers_present"
    assert promotion_harness.git.unmerged_paths(lease.worktree_path) == ("README.txt",)
    resolved.write_text("staging version\n", encoding="utf-8")

    delta = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert delta.status == "blocked"
    assert delta.blockers[0]["code"] == "sync_delta_mismatch"
    assert promotion_harness.git.unmerged_paths(lease.worktree_path) == ("README.txt",)
    resolved.write_text("resolved synchronization\n", encoding="utf-8")

    recovered = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert recovered.decision == "ready"


def test_recover_sync_requires_configured_production_verification(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    promotion_harness.configure(verify_production=())

    result = promotion_harness.service.recover_sync(lease.id)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "sync_verify_missing"


def test_sync_resumes_publication_of_recovered_two_parent_sync_commit(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    (lease.worktree_path / "README.txt").write_text(
        "resolved synchronization\n", encoding="utf-8"
    )
    promotion_harness.github.create_error = ExternalServiceError("temporary outage")

    blocked = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert blocked.status == "error"
    assert blocked.lease is not None
    assert blocked.lease.state is LeaseState.ACTIVE
    promotion_harness.github.create_error = None

    resumed = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.state is LeaseState.PR_OPEN
    assert promotion_harness.git.commit_parents(resumed.lease.head_sha) == (
        lease.target_base_sha,
        lease.source_head_sha,
    )


def test_sync_resumes_after_recovery_commit_transition_crash(
    promotion_harness: PromotionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    (lease.worktree_path / "README.txt").write_text(
        "resolved synchronization\n", encoding="utf-8"
    )
    original_transition = promotion_harness.registry.transition

    def fail_pending_transition(*args: object, **kwargs: object) -> Lease:
        if kwargs.get("event_type") == "sync_publish_pending":
            raise RuntimeError("simulated commit-transition crash")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(
        promotion_harness.registry, "transition", fail_pending_transition
    )

    interrupted = promotion_harness.service.recover_sync(lease.id, apply=True)

    assert interrupted.status == "blocked"
    assert interrupted.blockers[0]["code"] == "sync_recovery_failed"
    assert promotion_harness.git.commit_parents(
        promotion_harness.git.head_sha(lease.worktree_path)
    ) == (lease.target_base_sha, lease.source_head_sha)
    monkeypatch.setattr(promotion_harness.registry, "transition", original_transition)

    resumed = promotion_harness.service.sync(
        source_branch="main",
        target_branch="staging",
        apply=True,
    )

    assert resumed.decision == "ready"
    assert resumed.lease is not None
    assert resumed.lease.state is LeaseState.PR_OPEN


def test_recover_sync_rejects_stale_or_out_of_scope_conflict_changes(
    promotion_harness: PromotionHarness,
) -> None:
    lease = _blocked_sync_target_conflict(promotion_harness)
    (lease.worktree_path / "operator.txt").write_text(
        "operator\n", encoding="utf-8"
    )

    scoped = promotion_harness.service.recover_sync(lease.id)

    assert scoped.status == "blocked"
    assert scoped.blockers[0]["code"] == "sync_resolution_scope_mismatch"
    (lease.worktree_path / "operator.txt").unlink()
    _add_main_only_change(promotion_harness, path="later.txt", content="later\n")

    stale = promotion_harness.service.recover_sync(lease.id)

    assert stale.status == "blocked"
    assert "sync_lease_stale" in {
        blocker["code"] for blocker in stale.blockers
    }


def staged_main_feature(
    harness: Harness, *, staging_based: bool = False, production_ahead: bool = False,
) -> tuple[Lease, PullRequest]:
    """Keep a real origin feature head while staging contains unrelated content."""
    git_command(harness.repo, "branch", "main")
    git_command(harness.repo, "push", "-q", "origin", "main")
    (harness.repo / "README.txt").write_text("base\nstaging only\n", encoding="utf-8")
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "staging only")
    staging_base = harness.git.head_sha()
    git_command(harness.repo, "push", "-q", "origin", "staging")
    if production_ahead:
        git_command(harness.repo, "checkout", "-q", "main")
        (harness.repo / "README.txt").write_text("base\nproduction only\n", encoding="utf-8")
        git_command(harness.repo, "add", "README.txt")
        git_command(harness.repo, "commit", "-q", "-m", "production advance")
        git_command(harness.repo, "push", "-q", "origin", "main")
        git_command(harness.repo, "checkout", "-q", "staging")
    harness.service.config = replace(
        harness.service.config, production_branch="main", verify_production=(),
    )
    acquired = harness.service.acquire(
        initiative="solo-feature", purpose=Purpose.FEATURE,
        base="staging" if staging_based else None, branch=None, owner_id=None, apply=True,
    )
    assert acquired.lease is not None
    lease = acquired.lease
    content = "base\nstaging only\nfeature\n" if staging_based else "base\nfeature\n"
    if production_ahead:
        content = "base\nproduction only\nfeature\n"
    (lease.worktree_path / "README.txt").write_text(content, encoding="utf-8")
    git_command(lease.worktree_path, "add", "README.txt")
    git_command(lease.worktree_path, "commit", "-q", "-m", "solo feature")
    head = harness.git.head_sha(lease.worktree_path)
    git_command(lease.worktree_path, "push", "-q", "origin", lease.branch)
    # An explicit merge resolution retains both independent additions in staging,
    # without changing the reviewed original feature head.
    git_command(harness.repo, "merge", "--no-ff", "--no-commit", "-s", "ours", lease.branch)
    (harness.repo / "README.txt").write_text(
        "base\nstaging only\nfeature\n", encoding="utf-8"
    )
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "merged staging feature")
    merged = harness.git.head_sha()
    git_command(harness.repo, "push", "-q", "origin", "staging")
    source = replace(
        merged_pr(number=501, head_sha=head, changed_paths=("README.txt",)),
        base_sha=staging_base, head_ref=lease.branch, merge_commit_sha=merged,
        review_decision="", author_login="solo", merged_by_login="solo",
    )
    harness.github.prs[501] = source
    harness.github.created_head_sha = head
    return lease, source


def test_source_branch_promotes_tested_main_head_without_staging_or_local_mutation(
    harness: Harness,
) -> None:
    lease, source = staged_main_feature(harness)
    git_command(harness.repo, "checkout", "-q", "main")
    (harness.repo / "main-only.txt").write_text("another normal feature\n", encoding="utf-8")
    git_command(harness.repo, "add", "main-only.txt")
    git_command(harness.repo, "commit", "-q", "-m", "another production feature")
    git_command(harness.repo, "push", "-q", "origin", "main")
    (lease.worktree_path / "untracked.txt").write_text("ongoing local work\n", encoding="utf-8")
    before_worktrees = harness.git.list_worktrees()
    before_leases = harness.registry.list_leases_read_only()
    preview = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=False)
    assert preview.decision == "preview"
    assert harness.github.create_calls == []
    result = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert result.decision == "ready"
    assert result.lease is None
    assert result.actions[0]["source_branch"] == lease.branch
    assert result.actions[0]["tested_head"] == source.head_sha
    assert result.actions[0]["target_pr"] == 900
    assert harness.github.prs[900].head_ref == lease.branch
    assert harness.github.prs[900].head_sha == source.head_sha
    assert git_command(harness.repo, "show", f"{source.head_sha}:README.txt") == "base\nfeature"
    assert harness.git.remote_branch_sha(lease.branch) == source.head_sha
    assert harness.git.list_worktrees() == before_worktrees
    assert harness.registry.list_leases_read_only() == before_leases
    assert (lease.worktree_path / "untracked.txt").read_text() == "ongoing local work\n"


def test_source_branch_accepts_current_main_changes_in_the_same_reviewed_file(
    harness: Harness,
) -> None:
    lease, source = staged_main_feature(harness, production_ahead=True)

    result = harness.service.promote(
        source_pr=source.number, target_branch="main", source_branch=True, apply=True,
    )

    assert result.decision == "ready", result.blockers
    assert harness.github.prs[900].head_sha == source.head_sha
    assert git_command(harness.repo, "show", f"{source.head_sha}:README.txt") == (
        "base\nproduction only\nfeature"
    )
    assert harness.git.remote_branch_sha(lease.branch) == source.head_sha


def test_source_branch_rejects_same_file_staging_ancestry(harness: Harness) -> None:
    lease, source = staged_main_feature(harness, staging_based=True)
    assert source.changed_paths == ("README.txt",)
    result = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert result.blockers[0]["code"] == "source_branch_contains_staging"
    assert harness.github.create_calls == []
    assert harness.git.remote_branch_sha(lease.branch) == source.head_sha


@pytest.mark.parametrize("failure,code", [
    ("checks", "source_pr_checks_failed"),
    ("approved", "source_pr_not_approved"),
    ("other_merger", "source_pr_not_approved"),
    ("missing", "source_branch_unavailable"),
    ("head_drift", "source_branch_provenance_changed"),
    ("paths", "source_delta_mismatch"),
    ("protected", "source_pr_not_promotable"),
    ("sync", "source_pr_not_promotable"),
])
def test_source_branch_blocks_unproven_publication(
    harness: Harness, failure: str, code: str,
) -> None:
    lease, source = staged_main_feature(harness)
    if failure == "checks":
        harness.github.prs[501] = replace(source, checks_passed=False)
    elif failure == "approved":
        harness.service.config = replace(harness.service.config, source_review_policy="approved")
    elif failure == "other_merger":
        harness.github.prs[501] = replace(source, merged_by_login="someone-else")
    elif failure == "missing":
        git_command(harness.repo, "push", "-q", "origin", "--delete", lease.branch)
    elif failure == "head_drift":
        (lease.worktree_path / "later.txt").write_text("later\n", encoding="utf-8")
        git_command(lease.worktree_path, "add", "later.txt")
        git_command(lease.worktree_path, "commit", "-q", "-m", "later")
        git_command(lease.worktree_path, "push", "-q", "origin", lease.branch)
    elif failure == "paths":
        harness.github.prs[501] = replace(source, changed_paths=("other.txt",))
    elif failure == "protected":
        harness.github.prs[501] = replace(source, head_ref="main")
    else:
        harness.github.prs[501] = replace(source, body="AWF-No-Promote: true")
    result = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert result.blockers[0]["code"] == code
    assert harness.github.create_calls == []
    assert lease.worktree_path.is_dir()


@pytest.mark.parametrize("options", [
    {"source_pr": (501, 502)},
    {"source_pr": 501, "exclude_paths": ("README.txt",)},
    {"source_pr": 501, "out_of_order": True},
])
def test_source_branch_rejects_synthetic_mode_options(harness: Harness, options: dict) -> None:
    result = harness.service.promote(target_branch="main", source_branch=True, apply=True, **options)
    assert result.blockers[0]["code"] == "invalid_source_branch_promotion"
    assert harness.github.view_calls == []


def test_source_branch_reuses_only_exact_open_pr(harness: Harness) -> None:
    lease, source = staged_main_feature(harness)
    first = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert first.decision == "ready"
    reused = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert reused.decision == "reuse"
    assert len(harness.github.create_calls) == 1
    harness.github.open_prs[(lease.branch, "main")] = replace(
        harness.github.prs[900], head_sha=source.base_sha,
    )
    mismatch = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert mismatch.blockers[0]["code"] == "target_pr_mismatch"
    assert len(harness.github.create_calls) == 1


@pytest.mark.parametrize("drift", ("source", "target", "source_pr", "created_pr"))
def test_source_branch_preserves_publication_on_late_drift(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, drift: str,
) -> None:
    lease, source = staged_main_feature(harness)
    create = harness.github.create_pr

    def create_with_drift(**kwargs: str) -> PullRequest:
        published = create(**kwargs)
        if drift == "source_pr":
            harness.github.prs[501] = replace(source, base_sha=source.head_sha)
        elif drift == "created_pr":
            published = replace(published, head_sha=source.base_sha)
            harness.github.prs[900] = published
        else:
            branch = lease.branch if drift == "source" else "main"
            git_command(harness.repo, "push", "-q", "origin", f"staging:refs/heads/{branch}")
        return published

    monkeypatch.setattr(harness.github, "create_pr", create_with_drift)
    result = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert result.blockers[0]["code"] == (
        "target_pr_mismatch" if drift == "created_pr" else "source_branch_provenance_changed"
    )
    assert result.actions[0]["target_pr"] == 900
    assert harness.github.prs[900].state == "OPEN"
    assert lease.worktree_path.is_dir()


def test_staging_link_preserves_feature_until_final_production_link(harness: Harness) -> None:
    lease, source = staged_main_feature(harness)
    linked = harness.service.link_pr(lease.id, pr_number=501, apply=True)
    assert linked.decision == "ready"
    assert linked.lease.state is LeaseState.ACTIVE
    assert linked.lease.source_pr == 501
    assert linked.lease.source_head_sha == source.head_sha
    assert linked.lease.target_pr is None
    assert harness.service.finish(pr_number=501, apply=True).decision == "blocked"
    harness.service.status(refresh=True)
    assert lease.worktree_path.is_dir()
    assert harness.service.link_pr(lease.id, pr_number=501, apply=True).decision == "reuse"
    result = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert result.decision == "ready"
    harness.github.prs[900] = replace(
        harness.github.prs[900], state="MERGED", merge_commit_sha=source.head_sha,
    )
    final = harness.service.link_pr(lease.id, pr_number=900, apply=True)
    assert final.decision == "ready"
    assert final.lease.state is LeaseState.CLEANABLE
    assert final.lease.target_pr == 900
    assert harness.service.link_pr(lease.id, pr_number=900, apply=True).decision == "reuse"
    finished = harness.service.finish(pr_number=900, apply=True)
    assert finished.decision == "removed"
    assert not lease.worktree_path.exists()


def test_staging_evidence_requires_revalidation_after_new_commit(harness: Harness) -> None:
    lease, source = staged_main_feature(harness)
    assert harness.service.link_pr(lease.id, pr_number=501, apply=True).decision == "ready"
    (lease.worktree_path / "later.txt").write_text("later\n", encoding="utf-8")
    git_command(lease.worktree_path, "add", "later.txt")
    git_command(lease.worktree_path, "commit", "-q", "-m", "later feature")
    later_head = harness.git.head_sha(lease.worktree_path)
    stale = harness.service.promote(source_pr=501, target_branch="main", source_branch=True, apply=True)
    assert stale.blockers[0]["code"] == "staging_evidence_stale"
    harness.github.prs[901] = replace(
        source, number=901, base_ref="main", head_sha=later_head,
    )
    final = harness.service.link_pr(lease.id, pr_number=901, apply=True)
    assert final.blockers[0]["code"] == "staging_evidence_stale"
    git_command(lease.worktree_path, "push", "-q", "origin", lease.branch)
    base = harness.git.head_sha()
    git_command(harness.repo, "merge", "--no-ff", "-q", lease.branch, "-m", "restaged feature")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    harness.github.prs[502] = replace(
        source, number=502, base_sha=base, head_sha=later_head,
        merge_commit_sha=harness.git.head_sha(), changed_paths=("later.txt",),
    )
    refreshed = harness.service.link_pr(lease.id, pr_number=502, apply=True)
    assert refreshed.decision == "ready"
    assert refreshed.lease.source_pr == 502
    assert refreshed.lease.source_head_sha == later_head
    assert refreshed.lease.target_pr is None
    harness.github.created_head_sha = later_head
    promoted = harness.service.promote(
        source_pr=502, target_branch="main", source_branch=True, apply=True,
    )
    assert promoted.decision == "ready"
    assert promoted.actions[0]["source_prs"] == [501, 502]
    assert harness.github.prs[900].head_sha == later_head
    assert harness.service.link_pr(lease.id, pr_number=901, apply=True).decision == "ready"


@pytest.mark.parametrize("purpose,base,feature_base,production,expected", [
    (Purpose.FEATURE, None, None, "main", "main"),
    (Purpose.FEATURE, None, "feature-base", "main", "feature-base"),
    (Purpose.FEATURE, "staging", "feature-base", "main", "staging"),
    (Purpose.SCRATCH, None, "feature-base", "main", "staging"),
    (Purpose.FEATURE, None, None, None, "staging"),
])
def test_acquire_feature_base_precedence_keeps_other_purposes_on_staging(
    harness: Harness, purpose: Purpose, base: str | None,
    feature_base: str | None, production: str | None, expected: str,
) -> None:
    git_command(harness.repo, "branch", "main")
    git_command(harness.repo, "branch", "feature-base")
    git_command(harness.repo, "push", "-q", "origin", "main", "feature-base")
    harness.service.config = replace(
        harness.service.config, feature_base=feature_base, production_branch=production,
    )
    result = harness.service.acquire(
        initiative="base-choice", purpose=purpose, base=base,
        branch=None, owner_id=None, apply=True,
    )
    assert result.decision == "ready"
    assert result.lease.base_ref == f"origin/{expected}"


def test_source_branch_explicit_approval_allows_non_self_merged_source(harness: Harness) -> None:
    _, source = staged_main_feature(harness)
    harness.service.config = replace(harness.service.config, source_review_policy="approved")
    harness.github.prs[501] = replace(
        source, review_decision="APPROVED", merged_by_login="maintainer",
    )
    result = harness.service.promote(
        source_pr=501, target_branch="main", source_branch=True, apply=True,
    )
    assert result.decision == "ready"
    assert harness.github.prs[900].head_sha == source.head_sha


def test_source_branch_rechecks_target_immediately_before_creation(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, source = staged_main_feature(harness)
    find_open = harness.github.find_open_pr

    def find_with_drift(**kwargs: str) -> PullRequest | None:
        git_command(harness.repo, "push", "-q", "origin", "staging:refs/heads/main")
        return find_open(**kwargs)

    monkeypatch.setattr(harness.github, "find_open_pr", find_with_drift)
    result = harness.service.promote(
        source_pr=501, target_branch="main", source_branch=True, apply=True,
    )
    assert result.blockers[0]["code"] == "source_branch_provenance_changed"
    assert harness.github.create_calls == []
    assert harness.git.remote_branch_sha(lease.branch) == source.head_sha


def test_link_pr_no_promote_sync_feature_remains_final_cleanup_evidence(harness: Harness) -> None:
    git_command(harness.repo, "branch", "main")
    git_command(harness.repo, "push", "-q", "origin", "main")
    harness.service.config = replace(harness.service.config, production_branch="main")
    acquired = harness.service.acquire(
        initiative="sync-feature", purpose=Purpose.FEATURE, base="staging",
        branch="awf/sync-0123456789abcdef-0123456789ab/feature",
        owner_id=None, apply=True,
    )
    assert acquired.lease is not None
    lease = acquired.lease
    harness.github.prs[503] = replace(
        merged_pr(number=503, head_sha=lease.head_sha),
        head_ref=lease.branch, body="AWF-No-Promote: true",
    )
    result = harness.service.link_pr(lease.id, pr_number=503, apply=True)
    assert result.decision == "ready"
    assert result.lease.state is LeaseState.CLEANABLE
    assert result.lease.target_pr == 503
    assert result.lease.source_pr is None


def test_acquire_feature_falls_back_to_origin_default(harness: Harness) -> None:
    harness.service.config = WorktreeConfig()
    result = harness.acquire("remote-default")
    assert result.decision == "ready"
    assert result.lease.base_ref == "origin/staging"


def restaged_main_feature(harness: Harness) -> tuple[Lease, PullRequest, PullRequest]:
    lease, first = staged_main_feature(harness)
    assert harness.service.link_pr(lease.id, pr_number=501, apply=True).decision == "ready"
    (lease.worktree_path / "README.txt").write_text("base\nfeature corrected\n", encoding="utf-8")
    git_command(lease.worktree_path, "add", "README.txt")
    git_command(lease.worktree_path, "commit", "-q", "-m", "correct staging issue")
    head = harness.git.head_sha(lease.worktree_path)
    git_command(lease.worktree_path, "push", "-q", "origin", lease.branch)
    base = harness.git.head_sha()
    git_command(harness.repo, "merge", "--no-ff", "--no-commit", "-s", "ours", lease.branch)
    (harness.repo / "README.txt").write_text(
        "base\nstaging only\nfeature corrected\n", encoding="utf-8"
    )
    git_command(harness.repo, "add", "README.txt")
    git_command(harness.repo, "commit", "-q", "-m", "restage corrected feature")
    git_command(harness.repo, "push", "-q", "origin", "staging")
    latest = replace(
        first, number=502, base_sha=base, head_sha=head,
        merge_commit_sha=harness.git.head_sha(),
    )
    harness.github.prs[502] = latest
    harness.github.created_head_sha = head
    assert harness.service.link_pr(lease.id, pr_number=502, apply=True).decision == "ready"
    return lease, first, latest


def test_source_branch_restaging_chain_publishes_corrected_original_head(harness: Harness) -> None:
    lease, first, latest = restaged_main_feature(harness)
    result = harness.service.promote(
        source_pr=502, target_branch="main", source_branch=True, apply=True,
    )
    assert result.decision == "ready"
    assert result.actions[0]["source_prs"] == [501, 502]
    assert result.actions[0]["sources"][0]["head_sha"] == first.head_sha
    assert result.actions[0]["sources"][1]["head_sha"] == latest.head_sha
    assert harness.github.prs[900].head_ref == lease.branch
    assert harness.github.prs[900].head_sha == latest.head_sha
    assert harness.git.remote_branch_sha(lease.branch) == latest.head_sha
    assert git_command(harness.repo, "show", f"{latest.head_sha}:README.txt") == "base\nfeature corrected"
    assert "Validation-PR: #501" in harness.github.prs[900].body
    assert "Validation-PR: #502" in harness.github.prs[900].body


@pytest.mark.parametrize("failure,code", [
    ("missing", "source_branch_contains_staging"),
    ("ambiguous", "source_validation_chain_invalid"),
    ("checks", "source_pr_checks_failed"),
    ("approved", "source_pr_not_approved"),
    ("merge_order", "source_validation_chain_invalid"),
    ("paths", "source_delta_mismatch"),
    ("wrong_branch", "source_branch_contains_staging"),
])
def test_source_branch_restaging_chain_rejects_unproven_predecessor(
    harness: Harness, failure: str, code: str,
) -> None:
    lease, first, latest = restaged_main_feature(harness)
    if failure == "missing":
        del harness.github.prs[501]
    elif failure == "ambiguous":
        harness.github.prs[503] = replace(first, number=503)
    elif failure == "checks":
        harness.github.prs[501] = replace(first, checks_passed=False)
    elif failure == "approved":
        harness.service.config = replace(harness.service.config, source_review_policy="approved")
        harness.github.prs[502] = replace(latest, review_decision="APPROVED")
    elif failure == "merge_order":
        harness.github.prs[501] = replace(first, merge_commit_sha=latest.merge_commit_sha)
    elif failure == "paths":
        harness.github.prs[501] = replace(first, changed_paths=("other.txt",))
    else:
        harness.github.prs[501] = replace(first, head_ref="other-feature")
    result = harness.service.promote(
        source_pr=502, target_branch="main", source_branch=True, apply=True,
    )
    assert result.blockers[0]["code"] == code
    assert harness.github.create_calls == []
    assert harness.git.remote_branch_sha(lease.branch) == latest.head_sha


def test_source_branch_rechecks_historical_validation_after_publication(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, first, latest = restaged_main_feature(harness)
    create = harness.github.create_pr

    def create_with_old_pin_drift(**kwargs: str) -> PullRequest:
        published = create(**kwargs)
        harness.github.prs[501] = replace(first, base_sha=first.head_sha)
        return published

    monkeypatch.setattr(harness.github, "create_pr", create_with_old_pin_drift)
    result = harness.service.promote(
        source_pr=502, target_branch="main", source_branch=True, apply=True,
    )
    assert result.blockers[0]["code"] == "source_branch_provenance_changed"
    assert result.actions[0]["target_pr"] == 900
    assert harness.github.prs[900].state == "OPEN"
    assert harness.git.remote_branch_sha(lease.branch) == latest.head_sha


def test_github_client_merged_scan_returns_only_exact_validation_candidates(harness: Harness) -> None:
    from awf.worktrees.github import GhClient

    candidate = {
        "number": 42, "state": "MERGED", "baseRefName": "staging",
        "baseRefOid": "a" * 40, "headRefName": "feature/solo",
        "headRefOid": "b" * 40, "mergeCommit": {"oid": "c" * 40},
        "reviewDecision": "APPROVED", "statusCheckRollup": [], "files": [],
        "url": "https://github.example/acme/repo/pull/42",
    }
    payload = [
        candidate, {**candidate, "number": 43, "headRefName": "other"},
        {**candidate, "number": 44, "baseRefName": "main"},
        {**candidate, "number": 45, "state": "OPEN", "mergeCommit": None},
    ]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    matches = GhClient(harness.repo, command_runner=runner).find_merged_prs(
        head="feature/solo", base="staging",
    )
    assert [pull.number for pull in matches] == [42]
    assert matches[0].head_sha == "b" * 40


@pytest.mark.parametrize("payload", ([{}] * 100, {"items": []}, [None]))
def test_github_client_merged_scan_fails_closed_on_incomplete_or_invalid_results(
    harness: Harness, payload: object,
) -> None:
    from awf.worktrees.github import GhClient

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    with pytest.raises(ExternalServiceError):
        GhClient(harness.repo, command_runner=runner).find_merged_prs(
            head="feature/solo", base="staging",
        )