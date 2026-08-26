from __future__ import annotations

import io
import json
from dataclasses import replace
import subprocess
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from awf.commands.wt import _emit
from awf.cli import build_parser, main
from awf.worktrees.git import GitClient, GitError, GitRemoteError
from awf.worktrees.github import ExternalServiceError, PullRequest
from awf.worktrees.models import (
    CommandResult,
    DeploymentState,
    Lease,
    LeaseState,
    Purpose,
)
from awf.worktrees.registry import WorktreeRegistry
from worktree_fixtures import make_repository


def capture_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(argv)
    return result, stdout.getvalue(), stderr.getvalue()


def register_lease(db: Path, repo: Path, initiative: str) -> None:
    git = GitClient(repo)
    WorktreeRegistry(db).create_lease(
        Lease.new(
            repository_id=git.repository_id(),
            repository_name=git.repository_name(),
            repository_root=git.repository_root(),
            worktree_path=repo / f"worktree-{initiative}",
            initiative=initiative,
            purpose=Purpose.FEATURE,
            branch=f"awf/{initiative}/feature",
            base_ref="origin/staging",
            head_sha=git.head_sha(),
            managed=True,
            owner_kind="awf",
        )
    )


def register_cleanable_worktree_lease(
    db: Path, repo: Path, cache_dir: Path
) -> Lease:
    git = GitClient(repo)
    draft = Lease.new(
        repository_id=git.repository_id(),
        repository_name=git.repository_name(),
        repository_root=git.repository_root(),
        worktree_path=cache_dir / git.repository_name() / "draft",
        initiative="cache-path",
        purpose=Purpose.FEATURE,
        branch="awf/cache-path/feature",
        base_ref="origin/staging",
        head_sha=git.head_sha(),
        managed=True,
        owner_kind="awf",
    )
    lease = replace(
        draft,
        worktree_path=cache_dir / git.repository_name() / draft.id,
    )
    git.add_worktree(lease.worktree_path, lease.branch, lease.head_sha)
    registry = WorktreeRegistry(db)
    registry.create_lease(lease)
    opened = registry.transition(
        lease.id, LeaseState.PR_OPEN, expected_version=lease.version, pr_number=42
    )
    return registry.transition(
        lease.id,
        LeaseState.CLEANABLE,
        expected_version=opened.version,
        deployment_state=DeploymentState.NOT_REQUIRED,
    )


def merged_pull_request(lease: Lease, *, number: int = 42) -> PullRequest:
    return PullRequest(
        number=number,
        state="MERGED",
        base_ref=lease.base_ref,
        base_sha=lease.head_sha,
        head_ref=lease.branch,
        head_sha=lease.head_sha,
        merge_commit_sha=lease.head_sha,
        review_decision="APPROVED",
        checks_passed=True,
        changed_paths=(),
        url=f"https://github.example/acme/repo/pull/{number}",
    )


def create_adoptable_imported_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Lease:
    repo = make_repository(tmp_path)
    external = tmp_path / "legacy-release"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "legacy-release",
            str(external),
            "staging",
        ],
        cwd=repo,
        check=True,
    )
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    git = GitClient(external)
    return WorktreeRegistry(db).create_lease(
        Lease.new(
            repository_id=git.repository_id(),
            repository_name=git.repository_name(),
            repository_root=git.repository_root(),
            worktree_path=external,
            initiative="import-legacy-release-12345678",
            purpose=Purpose.SCRATCH,
            branch="legacy-release",
            base_ref="legacy-release",
            head_sha=git.head_sha(),
            managed=False,
            owner_kind="imported",
        )
    )


def create_linkable_managed_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Lease, str]:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    git = GitClient(repo)
    draft = Lease.new(
        repository_id=git.repository_id(),
        repository_name=git.repository_name(),
        repository_root=git.repository_root(),
        worktree_path=tmp_path / "cache" / "draft",
        initiative="managed-pr-link",
        purpose=Purpose.FEATURE,
        branch="awf/managed-pr-link/feature",
        base_ref="origin/staging",
        head_sha=git.head_sha(),
        managed=True,
        owner_kind="awf",
    )
    lease = replace(
        draft,
        worktree_path=tmp_path / "cache" / git.repository_name() / draft.id,
    )
    git.add_worktree(lease.worktree_path, lease.branch, lease.head_sha)
    lease = WorktreeRegistry(db).create_lease(lease)
    (lease.worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "feature.txt"],
        cwd=lease.worktree_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "feature"],
        cwd=lease.worktree_path,
        check=True,
    )
    return lease, git.head_sha(lease.worktree_path)


def test_wt_status_parser_surface() -> None:
    args = build_parser().parse_args(
        [
            "wt",
            "status",
            "--repo-root",
            "/repo",
            "--initiative",
            "reward",
            "--refresh",
            "--json",
        ]
    )

    assert args.command == "wt"
    assert args.wt_command == "status"
    assert args.repo_root == "/repo"
    assert args.initiative == "reward"
    assert args.json is True
    assert args.refresh is True


def test_wt_human_output_emits_refresh_warning_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = CommandResult.ok(
        "wt.status",
        decision="ready",
        warnings=(
            {
                "code": "github_refresh_failed",
                "message": "Unable to refresh pull request state for lease safe-id.",
            },
        ),
    )

    assert _emit(result, as_json=False) == 0

    captured = capsys.readouterr()
    assert captured.out == "wt.status: ready\n"
    assert captured.err == (
        "warning: github_refresh_failed: "
        "Unable to refresh pull request state for lease safe-id.\n"
    )


def test_wt_json_output_preserves_external_failure_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = CommandResult.error(
        "wt.promote",
        code="github_unavailable",
        message="gh authentication failed",
        exit_code=4,
    )

    assert _emit(result, as_json=True) == 4

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 4
    assert payload["blockers"] == [
        {"code": "github_unavailable", "message": "gh authentication failed"}
    ]


def test_wt_promote_maps_escaped_remote_failure_to_external_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repository(tmp_path)

    def remote_failure(*_args: object, **_kwargs: object) -> CommandResult:
        raise GitRemoteError("git push failed: network unavailable")

    monkeypatch.setattr(
        "awf.commands.wt.WorktreeService.promote",
        remote_failure,
    )

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "promote",
            "--source-pr",
            "372",
            "--to",
            "main",
            "--repo-root",
            str(repo),
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 4
    assert stderr == ""
    assert payload["status"] == "error"
    assert payload["exit_code"] == 4
    assert payload["blockers"][0]["code"] == "git_remote_error"


def test_wt_promote_keeps_local_git_failure_as_conflict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repository(tmp_path)

    def local_failure(*_args: object, **_kwargs: object) -> CommandResult:
        raise GitError("git rev-parse failed: local ref is malformed")

    monkeypatch.setattr(
        "awf.commands.wt.WorktreeService.promote",
        local_failure,
    )

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "promote",
            "--source-pr",
            "372",
            "--to",
            "main",
            "--repo-root",
            str(repo),
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 5
    assert stderr == ""
    assert payload["status"] == "error"
    assert payload["exit_code"] == 5
    assert payload["blockers"][0]["code"] == "git_error"


def test_wt_status_emits_one_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    repo = make_repository(tmp_path)

    rc, stdout, stderr = capture_main(
        ["wt", "status", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["schema_version"] == 1
    assert payload["command"] == "wt.status"
    assert payload["decision"] == "no_op"
    assert not db.parent.exists()


def test_wt_status_lists_registered_leases_without_an_initiative_filter(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "state.sqlite3"
    repo = make_repository(tmp_path)
    register_lease(db, repo, "reward")
    register_lease(db, repo, "metrics")
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))

    rc, stdout, stderr = capture_main(
        ["wt", "status", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["decision"] == "ready"
    assert {lease["initiative"] for lease in payload["leases"]} == {
        "reward",
        "metrics",
    }


def test_wt_status_filters_registered_leases_by_initiative(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "state.sqlite3"
    repo = make_repository(tmp_path)
    register_lease(db, repo, "reward")
    register_lease(db, repo, "metrics")
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "status",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["decision"] == "ready"
    assert [lease["initiative"] for lease in payload["leases"]] == ["reward"]


def test_wt_status_refresh_json_reports_pr_head_mismatch_as_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "state.sqlite3"
    repo = make_repository(tmp_path)
    register_lease(db, repo, "reward")
    registry = WorktreeRegistry(db)
    lease = registry.list_leases()[0]
    lease = registry.transition(
        lease.id,
        LeaseState.PR_OPEN,
        expected_version=lease.version,
        pr_number=42,
    )
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: merged_pull_request(
            replace(lease, head_sha="different-head-sha")
        ),
    )

    rc, stdout, stderr = capture_main(
        ["wt", "status", "--repo-root", str(repo), "--refresh", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["status"] == "ok"
    assert payload["leases"][0]["state"] == "BLOCKED"
    assert payload["leases"][0]["deployment_state"] == "unknown"
    assert payload["leases"][0]["head_sha"] == lease.head_sha


def test_wt_doctor_reports_unregistered_worktree_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))

    rc, stdout, _ = capture_main(
        ["wt", "doctor", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert payload["actions"][0]["kind"] == "unregistered_worktree"
    assert payload["actions"][0]["path"] == str(repo.resolve())
    assert not db.parent.exists()


def test_wt_doctor_human_output_lists_each_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    monkeypatch.setenv(
        "AWF_WORKTREE_STATE_DB", str(tmp_path / "state" / "worktrees.sqlite3")
    )
    home_dir = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_dir))

    rc, stdout, stderr = capture_main(
        ["wt", "doctor", "--repo-root", str(repo)]
    )

    assert rc == 0
    assert stderr == ""
    assert stdout == (
        "wt.doctor: preview\n"
        f"unregistered_worktree: {repo.resolve()}\n"
        "missing_skill_link: "
        f"{home_dir}/.claude/skills/release-worktree-lifecycle\n"
        "missing_skill_link: "
        f"{home_dir}/.agents/skills/release-worktree-lifecycle\n"
    )


def test_wt_acquire_parser_surface() -> None:
    args = build_parser().parse_args(
        [
            "wt",
            "acquire",
            "--initiative",
            "reward-widget",
            "--purpose",
            "scratch",
            "--repo-root",
            "/repo",
            "--base",
            "staging",
            "--branch",
            "topic/reward-widget",
            "--owner-id",
            "session-1",
            "--apply",
            "--json",
        ]
    )

    assert args.command == "wt"
    assert args.wt_command == "acquire"
    assert args.initiative == "reward-widget"
    assert args.purpose == "scratch"
    assert args.repo_root == "/repo"
    assert args.base == "staging"
    assert args.branch == "topic/reward-widget"
    assert args.owner_id == "session-1"
    assert args.apply is True
    assert args.json is True


def test_wt_promote_parser_surface() -> None:
    args = build_parser().parse_args(
        [
            "wt",
            "promote",
            "--source-pr",
            "372",
            "--source-pr",
            "373",
            "--exclude-path",
            "src/OpenApi.ts",
            "--exclude-path",
            "src/domains/fanLog/FanLogOpenApi.ts",
            "--out-of-order",
            "--to",
            "main",
            "--repo-root",
            "/repo",
            "--apply",
            "--json",
        ]
    )

    assert args.command == "wt"
    assert args.wt_command == "promote"
    assert args.source_pr == [372, 373]
    assert args.exclude_path == [
        "src/OpenApi.ts",
        "src/domains/fanLog/FanLogOpenApi.ts",
    ]
    assert args.to == "main"
    assert args.repo_root == "/repo"
    assert args.apply is True
    assert args.json is True
    assert args.out_of_order is True
    assert args.handler.__name__ == "run_wt_promote"


def test_wt_promote_forwards_out_of_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repository(tmp_path)
    received: dict[str, object] = {}

    def record_promotion(*_args: object, **kwargs: object) -> CommandResult:
        received.update(kwargs)
        return CommandResult.ok("wt.promote", decision="preview")

    monkeypatch.setattr(
        "awf.commands.wt.WorktreeService.promote",
        record_promotion,
    )

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "promote",
            "--source-pr",
            "372",
            "--out-of-order",
            "--to",
            "main",
            "--repo-root",
            str(repo),
            "--json",
        ]
    )

    assert rc == 0
    assert stderr == ""
    assert json.loads(stdout)["decision"] == "preview"
    assert received["out_of_order"] is True



def test_wt_recover_promotion_parser_surface() -> None:
    args = build_parser().parse_args(
        [
            "wt",
            "recover-promotion",
            "--lease",
            "lease-123",
            "--repo-root",
            "/repo",
            "--apply",
            "--json",
        ]
    )

    assert args.command == "wt"
    assert args.wt_command == "recover-promotion"
    assert args.lease == "lease-123"
    assert args.repo_root == "/repo"
    assert args.apply is True
    assert args.json is True
    assert args.handler.__name__ == "run_wt_recover_promotion"


def test_wt_recover_promotion_forwards_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repository(tmp_path)
    received: dict[str, object] = {}

    def record_recovery(
        _service: object, lease_id: str, *, apply: bool = False
    ) -> CommandResult:
        received["lease_id"] = lease_id
        received["apply"] = apply
        return CommandResult.ok("wt.recover-promotion", decision="recovered")

    monkeypatch.setattr(
        "awf.commands.wt.WorktreeService.recover_promotion",
        record_recovery,
    )

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "recover-promotion",
            "--lease",
            "lease-123",
            "--repo-root",
            str(repo),
            "--apply",
            "--json",
        ]
    )

    assert rc == 0
    assert stderr == ""
    assert json.loads(stdout)["decision"] == "recovered"
    assert received == {"lease_id": "lease-123", "apply": True}


def test_wt_acquire_preview_emits_one_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    cache_dir = tmp_path / "cache"
    (repo / ".awf").mkdir()
    (repo / ".awf" / "worktree.toml").write_text(
        "[worktree]\ndefault_base = \"staging\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(cache_dir))

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "acquire",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward-widget",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["command"] == "wt.acquire"
    assert payload["decision"] == "preview"
    assert payload["actions"][0]["kind"] == "create_worktree"
    assert len(GitClient(repo).list_worktrees()) == 1
    assert WorktreeRegistry(db).list_leases() == []
    assert not cache_dir.exists()


def test_wt_acquire_preview_reuses_without_mutating_existing_lease(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    cache_dir = tmp_path / "cache"
    (repo / ".awf").mkdir()
    (repo / ".awf" / "worktree.toml").write_text(
        "[worktree]\ndefault_base = \"staging\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(cache_dir))
    args = [
        "wt",
        "acquire",
        "--repo-root",
        str(repo),
        "--initiative",
        "reward-widget",
    ]

    first_rc, _, first_stderr = capture_main([*args, "--apply"])
    assert first_rc == 0
    assert first_stderr == ""
    registry = WorktreeRegistry(db)
    first = registry.find_active(
        GitClient(repo).repository_id(), "reward-widget", Purpose.FEATURE
    )
    assert first is not None
    before_events = registry.list_events(first.id)

    rc, stdout, stderr = capture_main([*args, "--json"])

    after = registry.get_lease(first.id)
    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["status"] == "ok"
    assert payload["decision"] == "reuse"
    assert payload["lease"]["id"] == first.id
    assert after == first
    assert after.version == first.version
    assert after.last_used_at == first.last_used_at
    assert after.head_sha == first.head_sha
    assert registry.list_events(first.id) == before_events


def test_wt_acquire_orphaned_lease_emits_blocked_json(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    cache_dir = tmp_path / "cache"
    (repo / ".awf").mkdir()
    (repo / ".awf" / "worktree.toml").write_text(
        "[worktree]\ndefault_base = \"staging\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(cache_dir))
    args = [
        "wt",
        "acquire",
        "--repo-root",
        str(repo),
        "--initiative",
        "reward-widget",
        "--apply",
    ]

    first_rc, _, first_stderr = capture_main(args)
    assert first_rc == 0
    assert first_stderr == ""
    lease = WorktreeRegistry(db).find_active(
        GitClient(repo).repository_id(), "reward-widget", Purpose.FEATURE
    )
    assert lease is not None
    subprocess.run(
        ["git", "worktree", "remove", str(lease.worktree_path)],
        cwd=repo,
        check=True,
    )

    rc, stdout, stderr = capture_main([*args[:-1], "--json"])

    payload = json.loads(stdout)
    assert rc == 3
    assert stderr == ""
    assert payload["status"] == "blocked"
    assert payload["blockers"][0]["code"] == "orphaned_lease"



def test_wt_acquire_rejects_unsafe_base_with_json_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    monkeypatch.setenv(
        "AWF_WORKTREE_STATE_DB", str(tmp_path / "state" / "worktrees.sqlite3")
    )
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(tmp_path / "cache"))

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "acquire",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward-widget",
            "--base",
            "main:refs/heads/unrelated",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 2
    assert stderr == ""
    assert payload["status"] == "error"
    assert payload["blockers"][0]["code"] == "config_error"
    assert len(GitClient(repo).list_worktrees()) == 1


def test_wt_acquire_lock_filesystem_error_emits_one_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    @contextmanager
    def denied_lock(*args, **kwargs):
        raise PermissionError("lock directory is read-only")
        yield

    repo = make_repository(tmp_path)
    monkeypatch.setenv(
        "AWF_WORKTREE_STATE_DB", str(tmp_path / "state" / "worktrees.sqlite3")
    )
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("awf.worktrees.service.repository_lock", denied_lock)

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "acquire",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward-widget",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 5
    assert stderr == ""
    assert payload["status"] == "error"
    assert payload["blockers"][0]["code"] == "filesystem_error"


def test_wt_import_parser_surface() -> None:
    args = build_parser().parse_args(
        ["wt", "import", "--root", "/repos", "--dry-run", "--json"]
    )

    assert args.command == "wt"
    assert args.wt_command == "import"
    assert args.root == "/repos"
    assert args.apply is False
    assert args.dry_run is True
    assert args.json is True


def test_wt_import_preview_emits_one_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    external = tmp_path / "legacy-release"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "legacy-release",
            str(external),
            "staging",
        ],
        cwd=repo,
        check=True,
    )
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))

    rc, stdout, stderr = capture_main(
        ["wt", "import", "--root", str(tmp_path), "--dry-run", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["command"] == "wt.import"
    assert payload["decision"] == "preview"
    assert any(lease["worktree_path"] == str(external.resolve()) for lease in payload["leases"])
    assert not db.parent.exists()


def test_wt_adopt_promotes_imported_lease_without_repo_argument(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    external = tmp_path / "legacy-release"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "legacy-release",
            str(external),
            "staging",
        ],
        cwd=repo,
        check=True,
    )
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    git = GitClient(external)
    lease = WorktreeRegistry(db).create_lease(
        Lease.new(
            repository_id=git.repository_id(),
            repository_name=git.repository_name(),
            repository_root=git.repository_root(),
            worktree_path=external,
            initiative="import-legacy-release-12345678",
            purpose=Purpose.SCRATCH,
            branch="legacy-release",
            base_ref="legacy-release",
            head_sha=git.head_sha(),
            managed=False,
            owner_kind="imported",
        )
    )

    rc, stdout, stderr = capture_main(
        ["wt", "adopt", "--lease", lease.id, "--apply", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["command"] == "wt.adopt"
    assert payload["decision"] == "ready"
    assert payload["lease"]["managed"] is True
    assert payload["lease"]["owner_kind"] == "imported"


def test_wt_adopt_apply_transition_conflict_is_registry_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = create_adoptable_imported_lease(tmp_path, monkeypatch)

    def fail_transition(*_args: object, **_kwargs: object) -> Lease:
        raise RuntimeError("registry temporarily unavailable")

    monkeypatch.setattr(WorktreeRegistry, "transition", fail_transition)

    rc, stdout, stderr = capture_main(
        ["wt", "adopt", "--lease", lease.id, "--apply", "--json"]
    )

    payload = json.loads(stdout)
    current = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3").get_lease(
        lease.id
    )
    assert rc == 5
    assert stderr == ""
    assert payload["command"] == "wt.adopt"
    assert payload["status"] == "error"
    assert payload["blockers"][0]["code"] == "registry_conflict"
    assert current == lease


def test_wt_adopt_with_merged_pr_previews_link_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = create_adoptable_imported_lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: merged_pull_request(lease, number=129),
    )

    rc, stdout, stderr = capture_main(
        ["wt", "adopt", "--lease", lease.id, "--pr", "129", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["command"] == "wt.adopt"
    assert payload["decision"] == "preview"
    assert payload["actions"][0]["pr_number"] == 129


def test_wt_adopt_with_merged_pr_applies_then_reuses_exact_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = create_adoptable_imported_lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: merged_pull_request(lease, number=129),
    )
    args = [
        "wt",
        "adopt",
        "--lease",
        lease.id,
        "--pr",
        "129",
        "--apply",
        "--json",
    ]

    first_rc, first_stdout, first_stderr = capture_main(args)
    repeat_rc, repeat_stdout, repeat_stderr = capture_main(args)

    first = json.loads(first_stdout)
    repeat = json.loads(repeat_stdout)
    assert first_rc == 0
    assert first_stderr == ""
    assert first["command"] == "wt.adopt"
    assert first["decision"] == "ready"
    assert first["lease"]["target_pr"] == 129
    assert first["lease"]["managed"] is True
    assert repeat_rc == 0
    assert repeat_stderr == ""
    assert repeat["command"] == "wt.adopt"
    assert repeat["decision"] == "reuse"
    assert repeat["lease"]["target_pr"] == 129
    assert repeat["lease"]["managed"] is True


@pytest.mark.parametrize("value", ("0", "-1", "not-a-number"))
def test_wt_adopt_rejects_invalid_pr_values_without_json_envelope(value: str) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with (
        redirect_stdout(stdout),
        redirect_stderr(stderr),
        pytest.raises(SystemExit) as exited,
    ):
        main(
            [
                "wt",
                "adopt",
                "--lease",
                "lease-id",
                "--pr",
                value,
                "--json",
            ]
        )

    assert exited.value.code == 2
    assert stdout.getvalue() == ""
    assert "value must be a positive integer" in stderr.getvalue()


def test_wt_adopt_pr_maps_github_provider_failure_to_external_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = create_adoptable_imported_lease(tmp_path, monkeypatch)

    def github_failure(_self: object, _number: int) -> PullRequest:
        raise ExternalServiceError("gh authentication required")

    monkeypatch.setattr("awf.worktrees.github.GhClient.view_pr", github_failure)

    rc, stdout, stderr = capture_main(
        ["wt", "adopt", "--lease", lease.id, "--pr", "129", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 4
    assert stderr == ""
    assert payload["status"] == "error"
    assert payload["blockers"][0]["code"] == "github_adopt_failed"


def test_wt_adopt_pr_blocks_provenance_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = create_adoptable_imported_lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: replace(
            merged_pull_request(lease, number=129),
            head_ref="different-branch",
        ),
    )

    rc, stdout, stderr = capture_main(
        ["wt", "adopt", "--lease", lease.id, "--pr", "129", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 3
    assert stderr == ""
    assert payload["status"] == "blocked"
    assert payload["blockers"][0]["code"] == "pr_branch_mismatch"


def test_wt_link_pr_previews_developed_head_without_repo_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, current_head = create_linkable_managed_lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: replace(
            merged_pull_request(lease, number=131),
            head_sha=current_head,
        ),
    )

    rc, stdout, stderr = capture_main(
        ["wt", "link-pr", "--lease", lease.id, "--pr", "131", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["command"] == "wt.link-pr"
    assert payload["decision"] == "preview"
    assert payload["actions"][0]["head_sha"] == current_head


def test_wt_link_pr_applies_then_reuses_exact_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, current_head = create_linkable_managed_lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: replace(
            merged_pull_request(lease, number=131),
            head_sha=current_head,
        ),
    )
    args = [
        "wt",
        "link-pr",
        "--lease",
        lease.id,
        "--pr",
        "131",
        "--apply",
        "--json",
    ]

    first_rc, first_stdout, first_stderr = capture_main(args)
    repeat_rc, repeat_stdout, repeat_stderr = capture_main(args)

    first = json.loads(first_stdout)
    repeat = json.loads(repeat_stdout)
    assert first_rc == 0
    assert first_stderr == ""
    assert first["decision"] == "ready"
    assert first["lease"]["target_pr"] == 131
    assert first["lease"]["head_sha"] == current_head
    assert first["lease"]["state"] == "CLEANABLE"
    assert first["lease"]["deployment_state"] == "not_required"
    assert repeat_rc == 0
    assert repeat_stderr == ""
    assert repeat["decision"] == "reuse"
    assert repeat["lease"] == first["lease"]


@pytest.mark.parametrize("value", ("0", "-1", "not-a-number"))
def test_wt_link_pr_rejects_invalid_pr_values_without_json_envelope(
    value: str,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with (
        redirect_stdout(stdout),
        redirect_stderr(stderr),
        pytest.raises(SystemExit) as exited,
    ):
        main(
            [
                "wt",
                "link-pr",
                "--lease",
                "lease-id",
                "--pr",
                value,
                "--json",
            ]
        )

    assert exited.value.code == 2
    assert stdout.getvalue() == ""
    assert "value must be a positive integer" in stderr.getvalue()


def test_wt_link_pr_unknown_lease_is_structured_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "AWF_WORKTREE_STATE_DB",
        str(tmp_path / "state" / "worktrees.sqlite3"),
    )

    rc, stdout, stderr = capture_main(
        ["wt", "link-pr", "--lease", "missing", "--pr", "131", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 3
    assert stderr == ""
    assert payload["command"] == "wt.link-pr"
    assert payload["blockers"][0]["code"] == "unknown_lease"


def test_wt_link_pr_maps_github_provider_failure_to_external_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, _ = create_linkable_managed_lease(tmp_path, monkeypatch)

    def github_failure(_self: object, _number: int) -> PullRequest:
        raise ExternalServiceError("gh authentication required")

    monkeypatch.setattr("awf.worktrees.github.GhClient.view_pr", github_failure)

    rc, stdout, stderr = capture_main(
        ["wt", "link-pr", "--lease", lease.id, "--pr", "131", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 4
    assert stderr == ""
    assert payload["command"] == "wt.link-pr"
    assert payload["blockers"][0]["code"] == "github_link_failed"


def test_wt_link_pr_blocks_provenance_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, current_head = create_linkable_managed_lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: replace(
            merged_pull_request(lease, number=131),
            head_ref="different-branch",
            head_sha=current_head,
        ),
    )

    rc, stdout, stderr = capture_main(
        ["wt", "link-pr", "--lease", lease.id, "--pr", "131", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 3
    assert stderr == ""
    assert payload["status"] == "blocked"
    assert payload["blockers"][0]["code"] == "pr_branch_mismatch"


def test_wt_finish_parser_surface() -> None:
    args = build_parser().parse_args(
        [
            "wt",
            "finish",
            "--repo-root",
            "/repo",
            "--pr",
            "42",
            "--apply",
            "--json",
        ]
    )

    assert args.wt_command == "finish"
    assert args.repo_root == "/repo"
    assert args.pr == 42
    assert args.apply is True
    assert args.json is True
    assert args.handler.__name__ == "run_wt_finish"


def test_wt_gc_parser_defaults_to_preview() -> None:
    args = build_parser().parse_args(
        ["wt", "gc", "--repo-root", "/repo", "--merged", "--older-than", "7d"]
    )

    assert args.wt_command == "gc"
    assert args.repo_root == "/repo"
    assert args.merged is True
    assert args.older_than == "7d"
    assert args.apply is False
    assert args.dry_run is False
    assert args.handler.__name__ == "run_wt_gc"


def test_wt_gc_rejects_apply_and_dry_run_together() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "wt",
                "gc",
                "--merged",
                "--older-than",
                "7d",
                "--apply",
                "--dry-run",
            ]
        )


@pytest.mark.parametrize("ancestor_symlink", [False, True])
def test_wt_finish_preserves_worktree_for_lexically_symlinked_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ancestor_symlink: bool
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    real_parent = tmp_path / "real-cache-parent"
    real_cache = real_parent / "cache"
    if ancestor_symlink:
        configured_parent = tmp_path / "configured-parent"
        configured_parent.symlink_to(real_parent, target_is_directory=True)
        configured_cache = configured_parent / "cache"
    else:
        configured_cache = tmp_path / "configured-cache"
        configured_cache.symlink_to(real_cache, target_is_directory=True)
    lease = register_cleanable_worktree_lease(db, repo, real_cache)
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(configured_cache))
    monkeypatch.setattr(
        "awf.worktrees.github.GhClient.view_pr",
        lambda _self, _number: merged_pull_request(lease),
    )

    rc, stdout, _ = capture_main(
        ["wt", "finish", "--repo-root", str(repo), "--pr", "42", "--apply", "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 3
    assert payload["blockers"][0]["code"] == "unsafe_worktree_path"
    assert lease.worktree_path.exists()