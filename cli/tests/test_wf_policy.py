"""Workflow policy acceptance tests — WF-TEST-001, 002, 003, 006.

Tests change class detection, HIL mapping, phase skip, gate auto-pass,
and risk-based investment differentiation.

Reference: docs/tests/workflow-and-multi-agent.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.state import (
    PHASE_GATE,
    PHASE_ORDER,
    _apply_policy_skips,
    apply_gate_result,
    detect_change_class,
    get_risk_investment,
    resolve_next_phase,
)
from awf.core.workflow_prompt import _validate_preconditions, is_hil_phase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(change_class: str = "standard", current_phase: str = "plan") -> dict:
    """Build a minimal workflow state dict for testing."""
    return {
        "id": "test-wf",
        "currentPhase": current_phase,
        "changeClass": change_class,
        "phases": {phase: {"status": "pending", "retries": 0} for phase in PHASE_ORDER},
        "gates": {
            "G1": {"passed": None},
            "G2": {"passed": None},
            "G3": {"passed": None},
            "G4": {"passed": None},
            "G5": {"passed": None},
            "G6": {"passed": None},
        },
        "history": [],
    }


def _make_agent_card(name: str, hil: bool = False, required_gates: dict | None = None) -> dict:
    card: dict = {"name": f"phase-{name}", "hil": hil}
    if required_gates:
        card["input"] = {"required_state": {"gates": required_gates}}
    return card


def _write_workflow_state(repo_root: Path, state: dict) -> None:
    wf_dir = repo_root / ".workflow"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ===========================================================================
# WF-TEST-001: change class → HIL 매핑
# ===========================================================================

def test_001_detect_change_class_high_risk():
    """HIGH_RISK_PATTERNS 키워드가 있으면 high_risk."""
    assert detect_change_class("사용자 인증 흐름 리팩토링") == "high_risk"
    assert detect_change_class("payment gateway 연동") == "high_risk"
    assert detect_change_class("Delete old migration files") == "high_risk"
    assert detect_change_class("production 배포 준비") == "high_risk"
    assert detect_change_class("k8s 클러스터 설정 변경") == "high_risk"


def test_001_detect_change_class_small():
    """30자 이하 + 고위험 키워드 없으면 small."""
    assert detect_change_class("오타 수정") == "small"
    assert detect_change_class("fix typo in readme") == "small"
    assert detect_change_class("버튼 색상 변경") == "small"


def test_001_detect_change_class_standard():
    """30자 초과 + 고위험 키워드 없으면 standard."""
    assert detect_change_class("사용자 프로필 페이지에 최근 활동 내역을 보여주는 타임라인 위젯 추가") == "standard"
    assert detect_change_class("Refactor the logging module to support structured JSON output format") == "standard"


def test_001_boundary_30_chars():
    """정확히 30자 → small, 31자 → standard."""
    text_30 = "a" * 30
    text_31 = "a" * 31
    assert len(text_30) == 30
    assert detect_change_class(text_30) == "small"
    assert detect_change_class(text_31) == "standard"


def test_001_empty_and_whitespace():
    """빈 문자열, 공백-only → small (30자 이하)."""
    assert detect_change_class("") == "small"
    assert detect_change_class("   ") == "small"
    assert detect_change_class("\n\t") == "small"


def test_001_high_risk_overrides_length():
    """고위험 키워드는 길이 무관하게 high_risk."""
    assert detect_change_class("auth") == "high_risk"  # 4자 but high_risk keyword


def test_001_hil_high_risk():
    """high_risk: hil=true 유지."""
    card = _make_agent_card("approve", hil=True)
    assert is_hil_phase(card, "high_risk") is True


def test_001_hil_standard():
    """standard: hil=true 유지."""
    card = _make_agent_card("approve", hil=True)
    assert is_hil_phase(card, "standard") is True


def test_001_hil_small_auto_approve():
    """small: hil=true → False (자동 승인 허용)."""
    card = _make_agent_card("approve", hil=True)
    assert is_hil_phase(card, "small") is False


def test_001_hil_small_done_exception():
    """small이라도 done phase는 hil=true 유지."""
    card = _make_agent_card("done", hil=True)
    assert is_hil_phase(card, "small") is True


def test_001_hil_false_always_false():
    """agent card hil=false면 change class 무관 False."""
    card = _make_agent_card("approve", hil=False)
    assert is_hil_phase(card, "high_risk") is False
    assert is_hil_phase(card, "standard") is False
    assert is_hil_phase(card, "small") is False


# ===========================================================================
# WF-TEST-002: phase skip
# ===========================================================================

def test_002_small_skips_review_approve_verify():
    """small → review, approve, verify skip."""
    state = _make_state("small")
    next_phase = resolve_next_phase(state)
    assert next_phase == "plan"

    # plan 완료 후
    state["phases"]["plan"]["status"] = "completed"
    state["currentPhase"] = "plan"
    next_phase = resolve_next_phase(state)
    # review/approve/verify가 skip되어 impl로
    assert next_phase == "impl"


def test_002_small_full_path():
    """small 전체 경로: plan → impl → test → done."""
    state = _make_state("small")
    executed: list[str] = []

    for _ in range(10):  # safety limit
        phase = resolve_next_phase(state)
        if phase in executed:
            break
        executed.append(phase)
        state["phases"][phase]["status"] = "completed"
        state["currentPhase"] = phase

    assert executed == ["plan", "impl", "test", "done"]


def test_002_standard_no_skip():
    """standard → 모든 phase 실행."""
    state = _make_state("standard")
    executed: list[str] = []

    for _ in range(10):
        phase = resolve_next_phase(state)
        if phase in executed:
            break
        executed.append(phase)
        state["phases"][phase]["status"] = "completed"
        state["currentPhase"] = phase

    assert executed == PHASE_ORDER


def test_002_high_risk_no_skip():
    """high_risk → 모든 phase 실행."""
    state = _make_state("high_risk")
    executed: list[str] = []

    for _ in range(10):
        phase = resolve_next_phase(state)
        if phase in executed:
            break
        executed.append(phase)
        state["phases"][phase]["status"] = "completed"
        state["currentPhase"] = phase

    assert executed == PHASE_ORDER


def test_002_skip_only_pending():
    """이미 completed인 phase는 skip하지 않음."""
    state = _make_state("small")
    state["phases"]["review"]["status"] = "completed"  # already done
    _apply_policy_skips(state)
    # review는 이미 completed → skip 대상이 아님
    assert state["phases"]["review"]["status"] == "completed"
    # approve, verify는 pending → skipped
    assert state["phases"]["approve"]["status"] == "skipped"
    assert state["phases"]["verify"]["status"] == "skipped"


# ===========================================================================
# WF-TEST-003: skip된 phase gate satisfaction
# ===========================================================================

def test_003_skipped_gate_auto_pass():
    """skip된 phase의 gate는 auto_pass=True."""
    state = _make_state("small")
    _apply_policy_skips(state)

    for phase in ["review", "approve", "verify"]:
        gate_id = PHASE_GATE[phase]
        gate = state["gates"][gate_id]
        assert gate["passed"] is True, f"{gate_id} should be passed"
        assert gate["auto_pass"] is True, f"{gate_id} should be auto_pass"
        assert gate["provider"] == "policy"
        assert "skip_reason" in gate


def test_003_skipped_phase_status():
    """skip된 phase의 status는 'skipped'."""
    state = _make_state("small")
    _apply_policy_skips(state)

    for phase in ["review", "approve", "verify"]:
        assert state["phases"][phase]["status"] == "skipped"
        assert "skippedAt" in state["phases"][phase]
        assert "skipReason" in state["phases"][phase]


def test_003_skipped_history():
    """skip된 phase는 history에 기록."""
    state = _make_state("small")
    _apply_policy_skips(state)

    skip_entries = [h for h in state["history"] if h["action"] == "skipped"]
    skipped_phases = {h["phase"] for h in skip_entries}
    assert skipped_phases == {"review", "approve", "verify"}


def test_003_precondition_with_auto_pass_gate():
    """auto_pass gate로 downstream precondition 통과."""
    state = _make_state("small")
    _apply_policy_skips(state)

    # G1 수동 통과 (plan 완료)
    state["gates"]["G1"]["passed"] = True

    # impl의 agent card: G3 필요 (approve가 skip → G3 auto_pass)
    impl_card = _make_agent_card("impl", required_gates={"G3": {"passed": True}})
    # 예외 없이 통과해야 함
    _validate_preconditions(state, impl_card, "impl")


def test_003_precondition_fails_without_gate():
    """gate가 미통과면 precondition 실패."""
    state = _make_state("standard")  # no skip → G3 is None

    impl_card = _make_agent_card("impl", required_gates={"G3": {"passed": True}})
    try:
        _validate_preconditions(state, impl_card, "impl")
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "G3" in str(e)


def test_agent_card_top_level_retry_max_controls_failure_abort(tmp_path: Path):
    """Agent card retry.max should be the runtime retry budget."""
    state = _make_state("standard", current_phase="review")
    _write_workflow_state(tmp_path, state)
    cards_dir = tmp_path / ".workflow" / "agent-cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    (cards_dir / "review.json").write_text(
        json.dumps(
            {
                "name": "phase-review",
                "retry": {"max": 2},
                "gate": {"id": "G2", "on_fail": {}},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    first = apply_gate_result(str(tmp_path), "review", False)
    assert first["phases"]["review"]["status"] == "failed"
    assert first["phases"]["review"]["retries"] == 1
    assert first["currentPhase"] == "review"

    second = apply_gate_result(str(tmp_path), "review", False)
    assert second["phases"]["review"]["status"] == "failed"
    assert second["phases"]["review"]["retries"] == 2
    assert second["currentPhase"] == "review"

    third = apply_gate_result(str(tmp_path), "review", False)
    assert third["phases"]["review"]["status"] == "aborted"
    assert third["phases"]["review"]["retries"] == 3
    assert third["currentPhase"] == "aborted"
    assert "max=2" in third["history"][-1]["details"]


def test_agent_card_on_fail_next_phase_replans_critical_failure(tmp_path: Path):
    """Agent card on_fail.*.next_phase should drive failure routing."""
    state = _make_state("standard", current_phase="review")
    state["phases"]["plan"]["status"] = "completed"
    state["phases"]["review"]["status"] = "in_progress"
    state["gates"]["G1"]["passed"] = True
    _write_workflow_state(tmp_path, state)
    cards_dir = tmp_path / ".workflow" / "agent-cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    (cards_dir / "review.json").write_text(
        json.dumps(
            {
                "name": "phase-review",
                "retry": {"max": 2},
                "gate": {
                    "id": "G2",
                    "on_fail": {"critical_found": {"next_phase": "plan"}},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    replanned = apply_gate_result(
        str(tmp_path),
        "review",
        False,
        failure_context={"has_critical": True},
    )

    assert replanned["currentPhase"] == "plan"
    assert replanned["phases"]["plan"]["status"] == "pending"
    assert replanned["phases"]["review"]["status"] == "pending"
    assert replanned["gates"]["G1"]["passed"] is None
    assert replanned["gates"]["G2"]["passed"] is None
    assert replanned["loop"]["replanCount"] == 1
    assert replanned["history"][-1]["action"] == "replanned"


def test_agent_card_on_fail_prompt_user_enters_deciding_state(tmp_path: Path):
    """Agent card on_fail.*.prompt_user should pause for user decision."""
    state = _make_state("standard", current_phase="review")
    state["phases"]["review"]["status"] = "in_progress"
    state["gates"]["G1"]["passed"] = True
    _write_workflow_state(tmp_path, state)
    cards_dir = tmp_path / ".workflow" / "agent-cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    (cards_dir / "review.json").write_text(
        json.dumps(
            {
                "name": "phase-review",
                "retry": {"max": 2},
                "gate": {
                    "id": "G2",
                    "on_fail": {"high_only": {"prompt_user": True}},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    deciding = apply_gate_result(
        str(tmp_path),
        "review",
        False,
        failure_context={"has_critical": False, "high_count": 1},
    )

    assert deciding["currentPhase"] == "review"
    assert deciding["phases"]["review"]["status"] == "deciding"
    assert deciding["phases"]["review"]["decision"] == "escalate_user"
    assert (
        deciding["phases"]["review"]["decisionReason"]
        == "agent-card on_fail.prompt_user matched"
    )
    assert deciding["phases"]["review"]["retries"] == 1
    assert deciding["gates"]["G2"]["passed"] is False
    pending = deciding["loop"]["pendingDecision"]
    assert pending["phase"] == "review"
    assert pending["decision"] == "escalate_user"
    assert deciding["history"][-1]["action"] == "deciding"


def test_agent_card_on_fail_failure_type_routes_named_branch(tmp_path: Path):
    """Agent card on_fail.<failure_type> should drive failure routing."""
    state = _make_state("standard", current_phase="verify")
    state["phases"]["approve"]["status"] = "completed"
    state["phases"]["impl"]["status"] = "completed"
    state["phases"]["verify"]["status"] = "in_progress"
    state["gates"]["G3"]["passed"] = True
    state["gates"]["G4"]["passed"] = True
    _write_workflow_state(tmp_path, state)
    cards_dir = tmp_path / ".workflow" / "agent-cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    (cards_dir / "verify.json").write_text(
        json.dumps(
            {
                "name": "phase-verify",
                "retry": {"max": 2},
                "gate": {
                    "id": "G5",
                    "on_fail": {"scope_violation": {"next_phase": "approve"}},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    replanned = apply_gate_result(
        str(tmp_path),
        "verify",
        False,
        failure_context={"failure_type": "scope_violation"},
    )

    assert replanned["currentPhase"] == "approve"
    assert replanned["phases"]["approve"]["status"] == "pending"
    assert replanned["phases"]["impl"]["status"] == "pending"
    assert replanned["phases"]["verify"]["status"] == "pending"
    assert replanned["gates"]["G3"]["passed"] is None
    assert replanned["gates"]["G4"]["passed"] is None
    assert replanned["gates"]["G5"]["passed"] is None
    assert replanned["loop"]["replanCount"] == 1
    assert replanned["history"][-1]["action"] == "replanned"


# ===========================================================================
# WF-TEST-006: risk-based 투자 차등
# ===========================================================================

def test_006_small_phase_path():
    """small: plan → impl → test → done."""
    state = _make_state("small")
    executed: list[str] = []
    for _ in range(10):
        phase = resolve_next_phase(state)
        if phase in executed:
            break
        executed.append(phase)
        state["phases"][phase]["status"] = "completed"
        state["currentPhase"] = phase
    assert executed == ["plan", "impl", "test", "done"]


def test_006_standard_full_path():
    """standard: 7-phase 전체."""
    state = _make_state("standard")
    executed: list[str] = []
    for _ in range(10):
        phase = resolve_next_phase(state)
        if phase in executed:
            break
        executed.append(phase)
        state["phases"][phase]["status"] = "completed"
        state["currentPhase"] = phase
    assert executed == PHASE_ORDER


def test_006_high_risk_full_path():
    """high_risk: 7-phase 전체."""
    state = _make_state("high_risk")
    executed: list[str] = []
    for _ in range(10):
        phase = resolve_next_phase(state)
        if phase in executed:
            break
        executed.append(phase)
        state["phases"][phase]["status"] = "completed"
        state["currentPhase"] = phase
    assert executed == PHASE_ORDER


def test_006_review_depth_differs():
    """change class별 review depth 차등."""
    small = get_risk_investment("small", "review")
    standard = get_risk_investment("standard", "review")
    high_risk = get_risk_investment("high_risk", "review")

    assert small["depth"] == "minimal"
    assert standard["depth"] == "standard"
    assert high_risk["depth"] == "deep"


def test_006_verify_strictness_differs():
    """change class별 verify strictness 차등."""
    small = get_risk_investment("small", "verify")
    standard = get_risk_investment("standard", "verify")
    high_risk = get_risk_investment("high_risk", "verify")

    assert small["strictness"] == "scope_only"
    assert standard["strictness"] == "scope_compliance"
    assert high_risk["strictness"] == "full"
    # high_risk는 skip_checks가 비어 있음
    assert high_risk["skip_checks"] == set()


def test_006_test_range_differs():
    """change class별 test range 차등."""
    small = get_risk_investment("small", "test")
    standard = get_risk_investment("standard", "test")
    high_risk = get_risk_investment("high_risk", "test")

    assert small["range"] == "related_only"
    assert standard["range"] == "regression_acceptance"
    assert high_risk["range"] == "full_with_manual"


def test_006_gate_scope_differs():
    """change class별 gate auto-pass 범위 차등: small만 review/approve/verify gate 자동 통과."""
    # small: 3개 gate auto-pass
    state_small = _make_state("small")
    _apply_policy_skips(state_small)
    auto_gates_small = {gid for gid, g in state_small["gates"].items() if g.get("auto_pass")}
    assert auto_gates_small == {"G2", "G3", "G5"}

    # standard: gate auto-pass 없음
    state_std = _make_state("standard")
    _apply_policy_skips(state_std)
    auto_gates_std = {gid for gid, g in state_std["gates"].items() if g.get("auto_pass")}
    assert auto_gates_std == set()

    # high_risk: gate auto-pass 없음
    state_hr = _make_state("high_risk")
    _apply_policy_skips(state_hr)
    auto_gates_hr = {gid for gid, g in state_hr["gates"].items() if g.get("auto_pass")}
    assert auto_gates_hr == set()


def test_006_unknown_change_class_fallback():
    """알 수 없는 change class → standard fallback."""
    inv = get_risk_investment("unknown_class", "review")
    standard_inv = get_risk_investment("standard", "review")
    assert inv == standard_inv


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
        group = name.split("_")[1]  # e.g., "001", "002", etc.
        if group != current_group:
            current_group = group
            print(f"\n--- WF-TEST-{group} ---")
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
