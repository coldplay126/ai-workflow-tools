from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.agent_runner import AgentResult
from awf.core.blackboard import Blackboard
from awf.core import team_runner
from awf.core.team_runner import (
    RoleConfig,
    TeamConfig,
    _build_worker_prompt,
    _execute_workers,
    _isolated_omp_preflight_error,
    run_team,
)


def _isolated_worker(role: str, scope: str, selector: str = "parallel") -> dict:
    return {
        "id": role,
        "provider": "claude-code",
        "isolated_omp": True,
        "task_selector": selector,
        "write_scope": [scope],
    }


def test_overlapping_isolated_omp_workers_fail_closed_by_default() -> None:
    config = TeamConfig.from_dict(
        {
            "name": "impl-overlap",
            "execution": "parallel",
            "roles": [
                _isolated_worker("one", "src/service"),
                _isolated_worker("two", "src/service/worker.py", "T012"),
            ],
        }
    )

    assert _isolated_omp_preflight_error("impl", config) == (
        "isolated_omp workers have overlapping or unprovable write_scope; "
        "set on_write_scope_overlap to 'sequential' or make scopes disjoint"
    )


def test_overlapping_isolated_omp_workers_use_explicit_sequential_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    config = TeamConfig.from_dict(
        {
            "name": "impl-overlap-sequential",
            "execution": "parallel",
            "on_write_scope_overlap": "sequential",
            "roles": [
                _isolated_worker("one", "src/service"),
                _isolated_worker("two", "src/service/worker.py", "T012"),
            ],
        }
    )
    blackboard = Blackboard.create(str(tmp_path), "impl")
    calls: list[str] = []

    def unexpected_parallel(*_args, **_kwargs):
        raise AssertionError("overlapping isolated OMP scopes must not dispatch in parallel")

    def sequential(*_args, **_kwargs):
        calls.append("sequential")
        return []

    monkeypatch.setattr(team_runner, "_execute_parallel", unexpected_parallel)
    monkeypatch.setattr(team_runner, "_execute_sequential", sequential)

    result = _execute_workers(
        blackboard,
        1,
        config,
        registry=None,
        cwd=str(tmp_path),
        timeout_sec=60,
        add_dirs=None,
        phase="impl",
    )

    assert result == []
    assert calls == ["sequential"]


def test_isolated_omp_worker_refuses_non_omp_dispatch_backend(monkeypatch, tmp_path: Path) -> None:
    config = TeamConfig.from_dict(
        {
            "name": "impl-omp-only",
            "roles": [_isolated_worker("writer", "src/writer.py", "T012")],
        }
    )
    blackboard = Blackboard.create(str(tmp_path), "impl")

    class NonOmpDispatch:
        name = "inline"

        def run(self, *_args, **_kwargs):
            raise AssertionError("isolated OMP work must not run on another backend")

    monkeypatch.setattr(team_runner, "_resolve_provider", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        team_runner,
        "_resolve_team_dispatch",
        lambda **_kwargs: NonOmpDispatch(),
    )

    results = team_runner._execute_sequential(
        blackboard,
        1,
        config,
        registry=None,
        cwd=str(tmp_path),
        timeout_sec=60,
        add_dirs=None,
        phase="impl",
    )

    assert len(results) == 1
    assert results[0].returncode == 2
    assert results[0].stderr == "isolated_omp worker requires the OMP dispatch backend"


def test_invalid_isolated_omp_lane_returns_failure_before_creating_team_workspace(
    tmp_path: Path,
) -> None:
    result = run_team(
        "impl",
        "implement the selected task",
        {
            "name": "invalid-isolated-lane",
            "roles": [
                {
                    "id": "writer",
                    "provider": "claude-code",
                    "isolated_omp": True,
                    "task_selector": "parallel",
                }
            ],
        },
        registry=None,
        cwd=str(tmp_path),
    )

    assert result.judge_verdict == "FAIL"
    assert "requires a non-empty write_scope" in result.judge_reason
    assert not (tmp_path / ".workflow").exists()


def test_worker_prompts_preserve_parent_ownership_and_test_namespace(tmp_path: Path) -> None:
    test_board = Blackboard.create(str(tmp_path), "test")
    test_board.write_mission("exercise acceptance criteria")

    test_prompt = _build_worker_prompt(
        test_board,
        2,
        RoleConfig(id="adversarial", provider="codex"),
    )

    assert "awf-test-turn-2-adversarial" in test_prompt
    assert "Do not share these resources" in test_prompt
    assert "not_run` or `skipped`" in test_prompt
    assert "parent alone merges canonical results and owns G6/HIL" in test_prompt

    plan_board = Blackboard.create(str(tmp_path), "plan")
    plan_board.write_mission("collect baseline facts")
    plan_prompt = _build_worker_prompt(
        plan_board,
        1,
        RoleConfig(
            id="baseline_research",
            provider="codex",
            baseline_research=True,
        ),
    )

    assert "Opt-in Baseline Research" in plan_prompt
    assert "parent planner/judge owns those decisions" in plan_prompt


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


def _initialize_git_checkout(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "team-runner@example.test")
    _git(root, "config", "user.name", "Team Runner Test")
    (root / "src").mkdir()
    (root / "src" / "allowed.py").write_text("original\n", encoding="utf-8")
    (root / "src" / "other.py").write_text("original\n", encoding="utf-8")
    (root / "canonical.md").write_text("original\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


def test_invalid_team_config_fails_before_workspace_or_dispatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        Blackboard,
        "create",
        staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError())),
    )

    result = run_team(
        "plan",
        "collect baseline facts",
        {
            "name": "invalid-read-only-team",
            "roles": [
                {
                    "id": "baseline_research",
                    "provider": "codex",
                    "baseline_research": True,
                    "write_scope": ["canonical.md"],
                }
            ],
        },
        registry=None,
        cwd=str(tmp_path),
    )

    assert result.judge_verdict == "FAIL"
    assert "invalid provider/team config" in result.judge_reason
    assert "must not declare write_scope" in result.judge_reason
    assert not (tmp_path / ".workflow").exists()


def test_read_only_worker_uses_isolation_and_fails_on_parent_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    _initialize_git_checkout(tmp_path)

    class MutatingOmpDispatch:
        name = "omp"

        def __init__(self) -> None:
            self.specs = []

        def run(self, specs, *, cwd: str, strategy: str):
            self.specs = specs
            assert strategy == "sequential"
            Path(cwd, "canonical.md").write_text("mutated\n", encoding="utf-8")
            return [
                AgentResult(
                    provider_name="codex",
                    role="baseline_research",
                    stdout='{"conclusion":"PASS","findings":[]}',
                    stderr="",
                    returncode=0,
                    elapsed_sec=0.0,
                )
            ]

    from awf.core import spec_loader

    monkeypatch.setattr(spec_loader, "resolve_agent_for_role", lambda _role: "analyzer")
    monkeypatch.setattr(
        spec_loader,
        "load_agent_definition",
        lambda _agent: {
            "meta": {"tools": "Read, Grep, Glob"},
            "instructions": "Return evidence only.",
        },
    )
    dispatch = MutatingOmpDispatch()
    monkeypatch.setattr(team_runner, "_resolve_provider", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        team_runner,
        "_resolve_team_dispatch",
        lambda **_kwargs: dispatch,
    )
    monkeypatch.setattr(team_runner, "_record_omp_team_provenance", lambda *_args, **_kwargs: None)

    result = run_team(
        "plan",
        "collect baseline facts",
        {
            "name": "baseline-read-only",
            "max_turns": 1,
            "roles": [
                {
                    "id": "baseline_research",
                    "protocol": "analyzer",
                    "provider": "codex",
                    "baseline_research": True,
                }
            ],
        },
        registry=None,
        cwd=str(tmp_path),
    )

    assert dispatch.specs[0].isolated is True
    assert dispatch.specs[0].agent_type == "analyzer"
    assert result.agents[0].returncode == 2
    assert result.agents[0].metadata["read_only_guard"]["valid"] is False
    assert "read_only_parent_mutation_detected" in result.agents[0].stderr


def test_read_only_role_rejects_resolved_agent_with_mutation_tool(monkeypatch) -> None:
    from awf.core import spec_loader

    monkeypatch.setattr(spec_loader, "resolve_agent_for_role", lambda _role: "unsafe")
    monkeypatch.setattr(
        spec_loader,
        "load_agent_definition",
        lambda _agent: {"meta": {"tools": "Read, Bash"}},
    )
    config = TeamConfig.from_dict(
        {
            "name": "unsafe-read-only",
            "roles": [
                {
                    "id": "baseline_research",
                    "provider": "codex",
                    "baseline_research": True,
                }
            ],
        }
    )

    assert "mutation-capable agent 'unsafe' with tools: bash" in str(
        team_runner._read_only_role_preflight_error(config)
    )


def test_isolated_selector_must_resolve_and_effective_scope_is_sealed(
    monkeypatch, tmp_path: Path
) -> None:
    sealed_tasks = team_runner._load_incomplete_tasks(
        "- [ ] T001 explicit task — src/allowed.py\n"
        "- [ ] T002 [P] parallel task — src/parallel.py\n"
    )
    monkeypatch.setattr(
        team_runner,
        "_load_sealed_impl_scope",
        lambda _cwd: (
            sealed_tasks,
            {"src/allowed.py", "src/parallel.py"},
            "sealed-identity",
        ),
    )

    explicit = TeamConfig.from_dict(
        {"name": "explicit", "roles": [_isolated_worker("writer", "src/**", "T001")]}
    )
    assert team_runner._restrict_isolated_omp_scopes(str(tmp_path), explicit) is None
    assert explicit.roles[0].selected_task_ids == ("T001",)
    assert explicit.roles[0].write_scope == ["src/allowed.py"]

    parallel = TeamConfig.from_dict(
        {"name": "parallel", "roles": [_isolated_worker("writer", "src/**")]}
    )
    assert team_runner._restrict_isolated_omp_scopes(str(tmp_path), parallel) is None
    assert parallel.roles[0].selected_task_ids == ("T002",)
    assert parallel.roles[0].write_scope == ["src/parallel.py"]

    mismatched = TeamConfig.from_dict(
        {"name": "mismatch", "roles": [_isolated_worker("writer", "src/**", "T099")]}
    )
    assert "does not resolve to an incomplete sealed task" in str(
        team_runner._restrict_isolated_omp_scopes(str(tmp_path), mismatched)
    )

    monkeypatch.setattr(
        team_runner,
        "_load_sealed_impl_scope",
        lambda _cwd: (sealed_tasks, {"src/other.py"}, "sealed-identity"),
    )
    outside_allowed = TeamConfig.from_dict(
        {"name": "outside", "roles": [_isolated_worker("writer", "src/**", "T001")]}
    )
    assert "outside G3-sealed allowed-files scope" in str(
        team_runner._restrict_isolated_omp_scopes(str(tmp_path), outside_allowed)
    )


def test_isolated_patch_rejects_phase_change_after_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from awf.core import state as workflow_state

    role = RoleConfig(
        id="writer",
        provider="codex",
        isolated_omp=True,
        write_scope=["src/allowed.py"],
        selected_task_ids=("T001",),
        planning_seal_identity="sealed-identity",
    )
    monkeypatch.setattr(
        workflow_state,
        "load_workflow_state",
        lambda _root: {"currentPhase": "verify"},
    )

    with pytest.raises(ValueError, match="workflow phase changed"):
        team_runner._validate_current_impl_seal(str(tmp_path), role)


def test_isolated_patch_rejects_out_of_scope_and_rename_before_apply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _initialize_git_checkout(tmp_path)
    monkeypatch.setattr(
        team_runner,
        "_validate_current_impl_seal",
        lambda *_args, **_kwargs: None,
    )
    other = tmp_path / "src" / "other.py"
    other.write_text("changed\n", encoding="utf-8")
    out_of_scope_patch = tmp_path / "out-of-scope.patch"
    out_of_scope_patch.write_text(
        _git(tmp_path, "diff", "--", "src/other.py"),
        encoding="utf-8",
    )
    other.write_text("original\n", encoding="utf-8")

    blackboard = Blackboard.create(
        str(tmp_path),
        "impl",
        team_config={"roles": [{"id": "writer", "write_scope": ["src/allowed.py"]}]},
    )
    role = RoleConfig(
        id="writer",
        provider="codex",
        isolated_omp=True,
        write_scope=["src/allowed.py"],
        selected_task_ids=("T001",),
    )
    out_of_scope_result = AgentResult(
        provider_name="codex",
        role="writer",
        stdout="",
        stderr="",
        returncode=0,
        elapsed_sec=0.0,
        metadata={"patch_path": str(out_of_scope_patch)},
    )
    team_runner._enforce_worker_write_scope(
        blackboard, role, out_of_scope_result, str(tmp_path)
    )
    assert out_of_scope_result.returncode == 2
    assert "isolated patch exceeds write_scope" in out_of_scope_result.stderr
    assert other.read_text(encoding="utf-8") == "original\n"
    allowed = tmp_path / "src" / "allowed.py"
    allowed.write_text("changed\n", encoding="utf-8")
    allowed_patch = tmp_path / "allowed.patch"
    allowed_patch.write_text(
        _git(tmp_path, "diff", "--", "src/allowed.py"),
        encoding="utf-8",
    )
    allowed.write_text("original\n", encoding="utf-8")
    allowed_result = AgentResult(
        provider_name="codex",
        role="writer",
        stdout="",
        stderr="",
        returncode=0,
        elapsed_sec=0.0,
        metadata={"patch_path": str(allowed_patch)},
    )
    team_runner._enforce_worker_write_scope(
        blackboard, role, allowed_result, str(tmp_path)
    )
    assert allowed_result.returncode == 0
    assert allowed_result.metadata["write_scope_validation"]["applied"] is True
    assert allowed.read_text(encoding="utf-8") == "changed\n"
    _git(tmp_path, "reset", "--hard", "HEAD")

    _git(tmp_path, "mv", "src/allowed.py", "src/renamed.py")
    rename_patch = tmp_path / "rename.patch"
    rename_patch.write_text(
        _git(tmp_path, "diff", "--cached", "--find-renames"),
        encoding="utf-8",
    )
    _git(tmp_path, "reset", "--hard", "HEAD")
    rename_result = AgentResult(
        provider_name="codex",
        role="writer",
        stdout="",
        stderr="",
        returncode=0,
        elapsed_sec=0.0,
        metadata={"patch_path": str(rename_patch)},
    )
    team_runner._enforce_worker_write_scope(
        blackboard, role, rename_result, str(tmp_path)
    )
    assert rename_result.returncode == 2
    assert "isolated patch renames are not permitted" in rename_result.stderr
    assert (tmp_path / "src" / "allowed.py").is_file()
    assert not (tmp_path / "src" / "renamed.py").exists()
