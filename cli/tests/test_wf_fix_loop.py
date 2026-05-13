"""§3.2 verify fix-loop guard tests."""

from __future__ import annotations

from awf.commands.wf import (
    VERIFY_FIX_LOOP_HARD_LIMIT,
    VERIFY_FIX_LOOP_WARN_THRESHOLD,
    _verify_fix_loop_status,
)


def _state(phase: str, executions: int) -> dict:
    return {
        "phases": {phase: {"executions": executions}},
    }


def test_non_verify_phase_always_ok() -> None:
    assert _verify_fix_loop_status(_state("impl", 99), "impl") == ("ok", 0)
    assert _verify_fix_loop_status(_state("review", 99), "review") == ("ok", 0)


def test_below_warn_threshold_is_ok() -> None:
    status, projected = _verify_fix_loop_status(_state("verify", 0), "verify")
    assert status == "ok"
    assert projected == 1


def test_at_warn_threshold() -> None:
    status, projected = _verify_fix_loop_status(
        _state("verify", VERIFY_FIX_LOOP_WARN_THRESHOLD - 1),
        "verify",
    )
    assert status == "warn"
    assert projected == VERIFY_FIX_LOOP_WARN_THRESHOLD


def test_just_below_hard_limit_is_warn() -> None:
    status, projected = _verify_fix_loop_status(
        _state("verify", VERIFY_FIX_LOOP_HARD_LIMIT - 1),
        "verify",
    )
    assert status == "warn"
    assert projected == VERIFY_FIX_LOOP_HARD_LIMIT


def test_at_hard_limit_aborts() -> None:
    status, projected = _verify_fix_loop_status(
        _state("verify", VERIFY_FIX_LOOP_HARD_LIMIT),
        "verify",
    )
    assert status == "abort"
    assert projected == VERIFY_FIX_LOOP_HARD_LIMIT + 1


def test_missing_executions_defaults_to_zero() -> None:
    status, projected = _verify_fix_loop_status({"phases": {"verify": {}}}, "verify")
    assert status == "ok"
    assert projected == 1


def test_missing_phase_entry_defaults_to_zero() -> None:
    status, projected = _verify_fix_loop_status({"phases": {}}, "verify")
    assert status == "ok"
    assert projected == 1
