"""Agent team acceptance tests — WF-TEST-005, MA-TEST-001.

WF-TEST-005: Cross-validation through team finding loop and termination.
MA-TEST-001: QA agent team — happy path + adversarial worker integration.

Tests Blackboard, TeamFinding, evaluate_termination, and _compute_verdict
with filesystem fixtures (no live provider calls).

Reference: docs/tests/workflow-and-multi-agent.md § WF-TEST-005, MA-TEST-001
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.agent_runner import AgentResult
from awf.core.blackboard import Blackboard, TeamFinding
from awf.core.team_runner import (
    RoleConfig,
    TeamConfig,
    _build_combined_output,
    _build_mission,
    _compute_verdict,
    _enforce_worker_write_scope,
    _record_omp_team_provenance,
    run_team,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bb(tmp: Path, phase: str = "plan") -> Blackboard:
    """Create a Blackboard in a temp .workflow/team/{phase}/ directory."""
    return Blackboard.create(str(tmp), phase)


def _write_findings_json(bb: Blackboard, turn: int, role: str, findings: list[dict]) -> None:
    """Convenience: write a structured findings dict to discussion/."""
    bb.write_findings(turn, role, {"conclusion": "PASS" if not findings else "FAIL", "findings": findings})


def _finding(severity: str = "INFO", category: str = "test", location: str = "f.py:1",
             description: str = "test finding", suggestion: str = "") -> dict:
    return {"severity": severity, "category": category, "location": location,
            "description": description, "suggestion": suggestion}


# ===========================================================================
# WF-TEST-005: 에이전트 팀 교차 검증
# ===========================================================================


def test_005_blackboard_create():
    """Blackboard 생성 → 디렉토리 구조 + meta.json."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "plan")
        assert bb.board_dir.exists()
        assert bb.discussion_dir.exists()
        assert bb.meta_path.exists()
        meta = json.loads(bb.meta_path.read_text())
        assert meta["phase"] == "plan"


def test_005_write_and_collect_findings():
    """worker가 findings를 쓰면 collect_findings로 수집 가능."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("HIGH", "spec_plan_mismatch", "spec.md:FR-003",
                     "FR-003 is in spec but not in plan"),
        ])
        findings = bb.collect_findings(1)
        assert len(findings) == 1
        assert findings[0].severity == "HIGH"
        assert findings[0].source_role == "spec_writer"
        assert findings[0].location == "spec.md:FR-003"


def test_005_cross_validation_detects_mismatch():
    """두 worker가 같은 불일치를 발견 → findings 합산."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        # spec_writer finds spec ↔ plan mismatch
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("HIGH", "spec_plan_mismatch", "spec.md:FR-003",
                     "FR-003 not implemented in plan"),
        ])
        # constitution_reviewer finds constitution violation
        _write_findings_json(bb, 1, "constitution_reviewer", [
            _finding("MAJOR", "constitution_violation", "plan.md:Phase-2",
                     "Phase 2 does not follow REST rule from constitution"),
        ])
        findings = bb.collect_findings(1)
        assert len(findings) == 2
        roles = {f.source_role for f in findings}
        assert roles == {"spec_writer", "constitution_reviewer"}


def test_005_termination_continues_on_critical():
    """CRITICAL/HIGH finding → should_stop=False (계속 수정)."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("CRITICAL", "missing_requirement", "spec.md:FR-001"),
        ])
        should_stop, reason = bb.evaluate_termination(1)
        assert not should_stop
        assert reason == "critical_findings"


def test_005_termination_continues_on_major_2():
    """MAJOR/MEDIUM 2건 이상 → should_stop=False."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("MAJOR", "gap1", "spec.md:1", "Gap one"),
            _finding("MEDIUM", "gap2", "plan.md:5", "Gap two"),
        ])
        should_stop, reason = bb.evaluate_termination(1)
        assert not should_stop
        assert "major_findings" in reason


def test_005_termination_stops_on_no_findings():
    """findings 없음 → should_stop=True (clean)."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [])
        should_stop, reason = bb.evaluate_termination(1)
        assert should_stop
        assert reason == "no_findings"


def test_005_termination_stops_on_minor_only():
    """LOW/INFO only → should_stop=True (pass)."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("LOW", "style", "plan.md:10", "Minor style issue"),
            _finding("INFO", "note", "tasks.md:1", "FYI"),
        ])
        should_stop, reason = bb.evaluate_termination(1)
        assert should_stop
        assert reason == "pass"


def test_005_termination_dedup_major():
    """같은 category:location의 MAJOR 2건 → dedup 후 1건 → PASS."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("MAJOR", "gap", "spec.md:1", "Same gap worker 1"),
        ])
        _write_findings_json(bb, 1, "constitution_reviewer", [
            _finding("MAJOR", "gap", "spec.md:1", "Same gap worker 2"),
        ])
        should_stop, reason = bb.evaluate_termination(1)
        assert should_stop  # dedup → only 1 major


def test_005_mission_includes_prev_findings():
    """Turn 2 mission에 Turn 1 findings가 포함됨."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("HIGH", "spec_mismatch", "spec.md:FR-003", "Mismatch detected"),
        ])
        config = TeamConfig(
            name="plan-team", roles=[
                RoleConfig(id="spec_writer", provider="claude-code"),
                RoleConfig(id="constitution_reviewer", provider="codex"),
            ], max_turns=3,
        )
        mission = _build_mission("Generate spec-kit artifacts", bb, 2, config)
        assert "Previous Findings" in mission
        assert "spec_mismatch" in mission
        assert "FR-003" in mission


def test_005_mission_no_prev_findings_turn1():
    """Turn 1 mission에는 previous findings 없음."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        config = TeamConfig(
            name="plan-team", roles=[
                RoleConfig(id="spec_writer", provider="claude-code"),
            ], max_turns=3,
        )
        mission = _build_mission("Generate spec-kit artifacts", bb, 1, config)
        assert "Previous Findings" not in mission
        assert "Generate spec-kit artifacts" in mission


def test_005_collect_all_findings_across_turns():
    """여러 턴의 findings를 전부 수집."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "spec_writer", [
            _finding("HIGH", "gap1", "spec.md:1"),
        ])
        _write_findings_json(bb, 2, "spec_writer", [
            _finding("LOW", "resolved", "spec.md:1"),
        ])
        all_findings = bb.collect_all_findings()
        assert len(all_findings) == 2
        turns = {f.turn for f in all_findings}
        assert turns == {1, 2}


# ===========================================================================
# MA-TEST-001: 에이전트 팀 QA — happy path + adversarial
# ===========================================================================


def test_001_happy_and_adversarial_different_findings():
    """happy path 에이전트와 adversarial 에이전트가 다른 findings 생성."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "test")

        # happy_path worker: basic tests pass
        _write_findings_json(bb, 1, "happy_path", [])

        # adversarial worker: finds edge case
        _write_findings_json(bb, 1, "adversarial", [
            _finding("HIGH", "null_handling", "service.py:42",
                     "null 입력 시 NullPointerException 발생",
                     "input validation 추가 필요"),
        ])

        findings = bb.collect_findings(1)
        assert len(findings) == 1  # adversarial found 1 issue
        assert findings[0].source_role == "adversarial"
        assert findings[0].category == "null_handling"


def test_001_adversarial_finds_more_than_happy():
    """adversarial이 happy path보다 더 많은 엣지 케이스 발견."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "test")

        _write_findings_json(bb, 1, "happy_path", [
            _finding("INFO", "basic_pass", "test.py:1", "All basic tests pass"),
        ])

        _write_findings_json(bb, 1, "adversarial", [
            _finding("HIGH", "null_crash", "service.py:42", "Null input crashes"),
            _finding("MAJOR", "boundary", "service.py:58", "Boundary value overflow"),
            _finding("MEDIUM", "race_condition", "service.py:100", "Potential race condition"),
        ])

        findings = bb.collect_findings(1)
        adversarial_findings = [f for f in findings if f.source_role == "adversarial"]
        happy_findings = [f for f in findings if f.source_role == "happy_path"]
        assert len(adversarial_findings) > len(happy_findings)
        assert len(adversarial_findings) == 3


def test_001_team_continues_with_critical_edge_case():
    """adversarial이 CRITICAL 발견 → 팀 계속 실행."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "test")

        _write_findings_json(bb, 1, "happy_path", [])
        _write_findings_json(bb, 1, "adversarial", [
            _finding("CRITICAL", "security", "auth.py:10", "SQL injection possible"),
        ])

        should_stop, reason = bb.evaluate_termination(1)
        assert not should_stop
        assert reason == "critical_findings"


def test_001_team_passes_after_fix():
    """Turn 2에서 모든 이슈 해결 → PASS."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "test")

        # Turn 1: adversarial finds issue
        _write_findings_json(bb, 1, "adversarial", [
            _finding("HIGH", "null_crash", "service.py:42"),
        ])
        should_stop1, _ = bb.evaluate_termination(1)
        assert not should_stop1

        # Turn 2: both pass
        _write_findings_json(bb, 2, "happy_path", [])
        _write_findings_json(bb, 2, "adversarial", [])
        should_stop2, reason = bb.evaluate_termination(2)
        assert should_stop2
        assert reason == "no_findings"


def test_001_merged_findings_from_both_roles():
    """두 역할의 findings가 통합되어 verdict 계산."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "test")

        _write_findings_json(bb, 1, "happy_path", [
            _finding("INFO", "coverage_ok", "test.py:1"),
        ])
        _write_findings_json(bb, 1, "adversarial", [
            _finding("MAJOR", "edge1", "svc.py:10", "Edge case 1"),
            _finding("MAJOR", "edge2", "svc.py:20", "Edge case 2"),
        ])

        # 2 MAJOR (different locations) → continue
        should_stop, reason = bb.evaluate_termination(1)
        assert not should_stop
        assert "major_findings" in reason


# ===========================================================================
# _compute_verdict (final team verdict)
# ===========================================================================


def test_verdict_pass_no_findings():
    verdict, _ = _compute_verdict([], "no_findings", 1, 3)
    assert verdict == "PASS"


def test_verdict_fail_critical():
    findings = [TeamFinding("CRITICAL", "bug", "a.py:1", "crash")]
    verdict, reason = _compute_verdict(findings, "pass", 1, 3)
    assert verdict == "FAIL"
    assert "critical" in reason


def test_verdict_fail_timeout():
    verdict, reason = _compute_verdict([], "timeout", 2, 3)
    assert verdict == "FAIL"
    assert "timed out" in reason


def test_verdict_fail_major_2():
    findings = [
        TeamFinding("MAJOR", "a", "x.py:1", "Issue A"),
        TeamFinding("MAJOR", "b", "y.py:2", "Issue B"),
    ]
    verdict, reason = _compute_verdict(findings, "pass", 1, 3)
    assert verdict == "FAIL"
    assert "major" in reason


def test_verdict_pass_major_dedup_1():
    """같은 category:location → dedup 후 1건 → PASS."""
    findings = [
        TeamFinding("MAJOR", "gap", "spec.md:1", "Finding A"),
        TeamFinding("MEDIUM", "gap", "spec.md:1", "Finding B"),
    ]
    verdict, _ = _compute_verdict(findings, "pass", 1, 3)
    assert verdict == "PASS"


def test_verdict_pass_max_turns_no_blocking():
    """max_turns 도달 but no critical/major → PASS."""
    findings = [TeamFinding("LOW", "style", "a.py:1", "minor")]
    verdict, reason = _compute_verdict(findings, "max_turns", 3, 3)
    assert verdict == "PASS"
    assert "no blocking" in reason


# ===========================================================================
# Blackboard utility tests
# ===========================================================================


def test_bb_write_scope_validation():
    """write_scope 설정 시 범위 외 경로는 거부 (repo root 상대경로)."""
    with tempfile.TemporaryDirectory() as tmp:
        team_config = {
            "roles": [
                {"id": "spec_writer", "provider": "claude-code",
                 "write_scope": [".workflow/team/plan/board/spec.md"]},
            ],
        }
        bb = Blackboard.create(str(tmp), "plan", team_config=team_config)
        # Inside scope
        assert bb.validate_write_scope("spec_writer", bb.board_dir / "spec.md")
        # Outside scope
        assert not bb.validate_write_scope("spec_writer", bb.board_dir / "plan.md")


def test_bb_write_scope_permissive_default():
    """write_scope 미설정 → 모든 경로 허용."""
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        assert bb.validate_write_scope("any_role", bb.board_dir / "anything.md")


def test_omp_team_provenance_failure_is_not_swallowed():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(RuntimeError, match="initialized .workflow"):
            _record_omp_team_provenance(
                tmp,
                backend="omp",
                strategy="parallel",
                phase="review",
                turn=1,
                agents=[],
                elapsed_sec=0.1,
            )


def test_bb_begin_run_clears_stale_turn_outputs():
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "verify")
        _write_findings_json(
            bb,
            1,
            "stale_role",
            [_finding("CRITICAL", "stale", "old.py:1", "old run")],
        )
        bb.write_mission("old mission", turn=1)
        (bb.board_dir / "stale.md").write_text("old board content", encoding="utf-8")

        bb.begin_run()

        assert bb.collect_findings(1) == []
        assert not bb.mission_path.exists()
        assert bb.list_board_artifacts() == []
        assert json.loads(bb.meta_path.read_text(encoding="utf-8"))["last_turn"] == 0


def test_team_combined_output_supplies_review_gate_coverage():
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "review")
        bb.write_findings(1, "reviewer-a", {
            "conclusion": "PASS",
            "findings": [],
            "coverage": {
                "total_requirements": 5,
                "mapped_requirements": 5,
                "percentage": 100,
                "gaps": [],
            },
        })
        bb.write_findings(1, "reviewer-b", {
            "conclusion": "PASS",
            "findings": [],
            "coverage": {
                "total_requirements": 5,
                "mapped_requirements": 4,
                "percentage": 80,
                "gaps": ["REQ-5"],
            },
        })

        payload = json.loads(_build_combined_output(
            bb,
            bb.collect_findings(1),
            phase="review",
            turn=1,
            verdict="PASS",
        ))
        assert payload["coverage"] == {
            "total_requirements": 5,
            "mapped_requirements": 4,
            "percentage": 80.0,
            "gaps": ["REQ-5"],
            "complete": True,
        }


def test_team_combined_output_fails_closed_on_missing_gate_metrics():
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp), "verify")
        bb.write_findings(1, "verifier-a", {
            "conclusion": "PASS",
            "findings": [],
            "scope": {"violations": 0},
            "compliance": {"fail": 0, "percentage": 100},
            "quality": {"critical": 0},
        })
        bb.write_findings(1, "verifier-b", {
            "conclusion": "PASS",
            "findings": [],
        })

        payload = json.loads(_build_combined_output(
            bb,
            bb.collect_findings(1),
            phase="verify",
            turn=1,
            verdict="PASS",
        ))
        assert payload["scope"]["complete"] is False
        assert payload["compliance"]["percentage"] == 0.0
        assert payload["quality"]["complete"] is False
        assert any("fail closed" in risk for risk in payload["risks"])


def _git_patch(repo: Path, relative_path: str, replacement: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = repo / relative_path
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", relative_path], cwd=repo, check=True)
    target.write_text(replacement, encoding="utf-8")
    diff = subprocess.run(
        ["git", "diff", "--", relative_path],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    target.write_text("before\n", encoding="utf-8")
    patch = repo / "worker.patch"
    patch.write_bytes(diff)
    return patch


def test_isolated_worker_patch_applies_only_within_write_scope():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        patch = _git_patch(repo, "allowed.txt", "after\n")
        team_config = {
            "roles": [{"id": "writer", "write_scope": ["allowed.txt"]}],
        }
        bb = Blackboard.create(str(repo), "impl", team_config=team_config)
        role = RoleConfig("writer", "omp", write_scope=["allowed.txt"])
        result = AgentResult(
            provider_name="omp",
            role="writer",
            stdout='{"conclusion":"PASS","findings":[]}',
            stderr="",
            returncode=0,
            elapsed_sec=0,
            metadata={"patch_path": str(patch)},
        )

        _enforce_worker_write_scope(bb, role, result, str(repo))
        assert result.ok
        assert (repo / "allowed.txt").read_text(encoding="utf-8") == "after\n"
        assert result.metadata["write_scope_validation"]["applied"] is True


def test_isolated_worker_patch_rejects_out_of_scope_path():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        patch = _git_patch(repo, "outside.txt", "after\n")
        team_config = {
            "roles": [{"id": "writer", "write_scope": ["allowed.txt"]}],
        }
        bb = Blackboard.create(str(repo), "impl", team_config=team_config)
        role = RoleConfig("writer", "omp", write_scope=["allowed.txt"])
        result = AgentResult(
            provider_name="omp",
            role="writer",
            stdout='{"conclusion":"PASS","findings":[]}',
            stderr="",
            returncode=0,
            elapsed_sec=0,
            metadata={"patch_path": str(patch)},
        )

        _enforce_worker_write_scope(bb, role, result, str(repo))
        assert not result.ok
        assert (repo / "outside.txt").read_text(encoding="utf-8") == "before\n"
        assert "exceeds write_scope" in result.stderr


def test_bb_has_critical_findings():
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "worker", [_finding("HIGH", "bug")])
        assert bb.has_critical_findings(1)


def test_bb_major_count():
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        _write_findings_json(bb, 1, "worker", [
            _finding("MAJOR", "a"), _finding("MEDIUM", "b"), _finding("LOW", "c"),
        ])
        assert bb.major_count(1) == 2


def test_bb_summary():
    with tempfile.TemporaryDirectory() as tmp:
        bb = _make_bb(Path(tmp))
        (bb.board_dir / "spec.md").write_text("# Spec", encoding="utf-8")
        _write_findings_json(bb, 1, "worker", [_finding("HIGH", "bug")])
        summary = bb.summary()
        assert summary["phase"] == "plan"
        assert "spec.md" in summary["artifacts"]
        assert summary["findings_total"] == 1
        assert summary["findings_critical"] == 1


# ===========================================================================
# run_team() integration with fake provider (Finding 2 fix)
# ===========================================================================


class _FakeResult:
    """Minimal ProviderResult-compatible object."""

    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode
        self.usage = None


class _FakeProvider:
    """Fake provider returning canned JSON output."""

    def __init__(self, name: str, output: dict):
        self.name = name
        self._output = json.dumps(output)

    def complete(self, prompt: str, cwd: str | None = None, add_dirs: list | None = None):
        return _FakeResult(self._output)


class _FakeRegistry:
    """Fake registry mapping provider names to _FakeProvider instances."""

    def __init__(self, providers: dict[str, _FakeProvider]):
        self._providers = providers

    def supports(self, name: str) -> bool:
        return name in self._providers

    def get(self, name: str):
        return self._providers[name]


def test_run_team_full_pass():
    """run_team() with fake providers — all workers PASS → team PASS."""
    with tempfile.TemporaryDirectory() as tmp:
        provider_a = _FakeProvider("fake-a", {"conclusion": "PASS", "findings": []})
        provider_b = _FakeProvider("fake-b", {"conclusion": "PASS", "findings": []})
        registry = _FakeRegistry({"fake-a": provider_a, "fake-b": provider_b})

        team_config = {
            "name": "test-team",
            "roles": [
                {"id": "happy_path", "provider": "fake-a"},
                {"id": "adversarial", "provider": "fake-b"},
            ],
            "execution": "sequential",
            "max_turns": 2,
            "timeout_sec": 30,
        }

        result = run_team("test", "Test prompt", team_config, registry, str(tmp))
        assert result.judge_verdict == "PASS"
        assert len(result.agents) >= 2


def test_run_team_critical_finding_fail():
    """run_team() — adversarial finds CRITICAL → FAIL after max turns."""
    with tempfile.TemporaryDirectory() as tmp:
        provider_pass = _FakeProvider("fake-pass", {"conclusion": "PASS", "findings": []})
        provider_fail = _FakeProvider("fake-fail", {
            "conclusion": "FAIL",
            "findings": [{"severity": "CRITICAL", "category": "security",
                          "location": "auth.py:10", "description": "SQL injection"}],
        })
        registry = _FakeRegistry({"fake-pass": provider_pass, "fake-fail": provider_fail})

        team_config = {
            "name": "qa-team",
            "roles": [
                {"id": "happy_path", "provider": "fake-pass"},
                {"id": "adversarial", "provider": "fake-fail"},
            ],
            "execution": "sequential",
            "max_turns": 2,
            "timeout_sec": 30,
        }

        result = run_team("test", "Test prompt", team_config, registry, str(tmp))
        assert result.judge_verdict == "FAIL"
        assert "critical" in result.judge_reason


def test_run_team_unavailable_provider():
    """run_team() — provider unavailable → synthetic CRITICAL finding → FAIL."""
    with tempfile.TemporaryDirectory() as tmp:
        registry = _FakeRegistry({})  # no providers available

        team_config = {
            "name": "broken-team",
            "roles": [
                {"id": "worker", "provider": "nonexistent"},
            ],
            "execution": "sequential",
            "max_turns": 1,
            "timeout_sec": 30,
        }

        result = run_team("test", "Test prompt", team_config, registry, str(tmp))
        assert result.judge_verdict == "FAIL"


def test_run_team_no_roles():
    """run_team() — no roles → immediate FAIL."""
    with tempfile.TemporaryDirectory() as tmp:
        registry = _FakeRegistry({})
        team_config = {"name": "empty-team", "roles": []}
        result = run_team("test", "Test prompt", team_config, registry, str(tmp))
        assert result.judge_verdict == "FAIL"
        assert "no roles" in result.judge_reason


# ===========================================================================
# Runner
# ===========================================================================


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    current_group = ""

    for test_fn in tests:
        name = test_fn.__name__
        group_prefix = name.split("_")[1]
        if group_prefix != current_group:
            current_group = group_prefix
            label = {
                "005": "WF-TEST-005: agent team cross-validation",
                "001": "MA-TEST-001: agent team QA",
                "verdict": "_compute_verdict",
                "bb": "Blackboard utilities",
            }.get(current_group, current_group)
            print(f"\n--- {label} ---")
        try:
            test_fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {exc}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
