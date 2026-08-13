"""Analysis Stage 3 acceptance tests — AN-STAGE3-001, AN-A3-001.

AN-STAGE3-001: Stage 3 trigger rule truth table (canonical rule).
AN-A3-001: Stage 3 resume contract — failure recovery, retry limits.

Tests the Stage 3 activation conditions and resume logic with fixture data.
No live provider calls required.

Reference: docs/tests/analysis-and-system.md § AN-STAGE3-001, AN-A3-001
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.analysis_resume import finalize_analysis_run, resolve_analysis_resume
from awf.core.analysis_state import should_run_stage3


# ===========================================================================
# AN-STAGE3-001: Stage 3 trigger truth table
# ===========================================================================


def test_stage3_001_deep_no_skip_runs():
    """deep + routing=run → Stage 3 실행."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=False,
        stage_routing_stage3="run", related_domains_count=0,
    )
    assert run
    assert reason == "routing_default"


def test_stage3_001_deep_skip_no_force_no_related():
    """deep + routing=skip + force=False + related<3 → Stage 3 skip."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=False,
        stage_routing_stage3="skip", related_domains_count=0,
    )
    assert not run
    assert "skipped" in reason


def test_stage3_001_deep_skip_force_overrides():
    """deep + routing=skip + force=True → Stage 3 실행 (force override)."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=True,
        stage_routing_stage3="skip", related_domains_count=0,
    )
    assert run
    assert reason == "force_enabled"


def test_stage3_001_deep_skip_related_3_overrides():
    """deep + routing=skip + related_domains=3 → Stage 3 실행 (auto-enable)."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=False,
        stage_routing_stage3="skip", related_domains_count=3,
    )
    assert run
    assert "auto_enabled" in reason
    assert "3" in reason


def test_stage3_001_deep_skip_related_5_overrides():
    """deep + routing=skip + related_domains=5 → Stage 3 실행."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=False,
        stage_routing_stage3="skip", related_domains_count=5,
    )
    assert run


def test_stage3_001_deep_skip_related_2_not_enough():
    """deep + routing=skip + related_domains=2 → Stage 3 skip (threshold=3)."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=False,
        stage_routing_stage3="skip", related_domains_count=2,
    )
    assert not run


def test_stage3_001_standard_mode_never_runs():
    """mode != deep → Stage 3 never runs."""
    for mode in ("standard", "quick", "document", "review", "investigate"):
        run, reason = should_run_stage3(
            mode=mode, stage3_force=True,
            stage_routing_stage3="run", related_domains_count=10,
        )
        assert not run, f"mode={mode} should not run stage3"
        assert reason == "not_deep_mode"


def test_stage3_001_retry_blocked():
    """deep + retry_blocked → Stage 3 skip."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=True,
        stage_routing_stage3="run", related_domains_count=10,
        stage3_retry_blocked=True,
    )
    assert not run
    assert reason == "retry_blocked"


def test_stage3_001_force_with_run_routing():
    """deep + routing=run + force=True → 실행 (force는 skip override용이므로 run에선 무관)."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=True,
        stage_routing_stage3="run", related_domains_count=0,
    )
    assert run
    assert reason == "routing_default"  # force only matters when routing=skip


def test_stage3_001_none_routing_is_not_skip():
    """routing=None → skip이 아니므로 실행."""
    run, reason = should_run_stage3(
        mode="deep", stage3_force=False,
        stage_routing_stage3=None, related_domains_count=0,
    )
    assert run


def test_stage3_001_truth_table_exhaustive():
    """Truth table: mode × force × routing × related_domains → 기대값 일치."""
    # (mode, force, routing, related_count) → expected_run
    cases = [
        # deep mode combinations
        ("deep", False, "skip", 0, False),
        ("deep", False, "skip", 2, False),
        ("deep", False, "skip", 3, True),    # auto-enable threshold
        ("deep", False, "skip", 5, True),
        ("deep", True,  "skip", 0, True),    # force override
        ("deep", True,  "skip", 3, True),    # force + related both active
        ("deep", False, "run",  0, True),
        ("deep", False, "run",  3, True),
        ("deep", True,  "run",  0, True),
        ("deep", False, None,   0, True),    # None != "skip"
        # non-deep always False
        ("standard", False, "run", 0, False),
        ("standard", True, "run",  5, False),
        ("quick",    False, "run", 0, False),
    ]
    for mode, force, routing, related, expected in cases:
        run, reason = should_run_stage3(
            mode=mode, stage3_force=force,
            stage_routing_stage3=routing, related_domains_count=related,
        )
        assert run == expected, (
            f"FAIL: mode={mode} force={force} routing={routing} "
            f"related={related} → got {run}, expected {expected} (reason={reason})"
        )


# ===========================================================================
# AN-A3-001: Stage 3 resume contract
# ===========================================================================


class _FakeContext:
    """Minimal analysis context for resume tests."""

    def __init__(self, tmp: Path, mode: str = "deep"):
        self.ai_context_dir = tmp / ".ai-context"
        self.ai_context_dir.mkdir(parents=True, exist_ok=True)
        (self.ai_context_dir / ".tmp").mkdir(exist_ok=True)
        self.repo_root = tmp
        self.docs_root = tmp / "docs"
        self.github_root = tmp / ".github"
        self.analysis_config_path = tmp / "analysis.json"
        self.analysis_pipeline_path = tmp / "pipeline.json"
        self.service = "test-svc"
        self.domain = "test-domain"
        self.mode = mode
        self.analysis_mode = "document"
        self.related_domains: list[str] = []
        self.domain_directories: list[str] = []
        self.all_directories: dict[str, list[str]] = {}
        self.existing_docs: list[str] = []
        self.include_patterns: list[str] | None = None
        self.config_path = tmp / "config.json"


def _make_analysis_state(
    *,
    stage3_status: str = "pending",
    stage3_retry_count: int = 0,
    stage3_error: str = "",
    stage2_status: str = "completed",
    output_status: str = "pending",
) -> dict:
    """Build a minimal analysis state dict."""
    return {
        "id": "test-analysis",
        "service": "test-svc",
        "domain": "test-domain",
        "mode": "deep",
        "scale": "standard",
        "startedAt": "2026-04-13T00:00:00+09:00",
        "completedAt": None,
        "currentLayer": "analyze",
        "currentStage": 2,
        "layers": {
            "input": {"status": "completed"},
            "bundle": {"status": "completed", "fileCount": 10, "lineCount": 500, "tokenEstimate": 1000},
            "analyze": {
                "stage1": {"status": "completed", "provider": "codex", "errorMessage": "", "retryCount": 0,
                           "observation": {"total_files": 10, "cached": 5, "analyzed": 5, "cache_hit_rate": 0.5}},
                "stage2": {"status": stage2_status, "provider": "opus", "errorMessage": "", "retryCount": 0},
                "stage3": {"status": stage3_status, "provider": "opus", "reason": "",
                           "errorMessage": stage3_error, "retryCount": stage3_retry_count},
            },
            "output": {"status": output_status, "errorMessage": ""},
        },
        "summaries": {"stage1": "done", "stage2": "done", "stage3": ""},
        "artifacts": {
            "domain_bundle": None, "project_bundle": None,
            "stage1_memo": ".tmp/stage1-analysis.md",
            "stage2_draft": ".tmp/stage2-draft.md",
            "stage3_final": ".tmp/stage3-final.md",
            "prompt_file": None, "result_file": None,
            "fanout_synthesizer_prompt": None, "fanout_writer_prompts": {},
        },
    }


def _save_state(ctx: _FakeContext, state: dict) -> None:
    state_path = ctx.ai_context_dir / ".analysis-state.json"
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_state(ctx: _FakeContext) -> dict:
    state_path = ctx.ai_context_dir / ".analysis-state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


def test_a3_001_stage3_in_progress_reset_to_pending():
    """stage3 status=in_progress (interrupted) → resume resets to pending."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage3_status="in_progress")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        resume_state = result["state"]
        assert resume_state["layers"]["analyze"]["stage3"]["status"] == "pending"
        assert any("in_progress" in m for m in result["messages"])


def test_a3_001_stage3_failed_retry_allowed():
    """stage3 failed + retryCount=0 → pending으로 복구, 재시도 허용."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage3_status="failed", stage3_retry_count=0,
                                      stage3_error="provider timeout")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        assert not result["stage3_retry_blocked"]
        resume_state = result["state"]
        assert resume_state["layers"]["analyze"]["stage3"]["status"] == "pending"
        assert any("retry" in m and "stage3" in m for m in result["messages"])


def test_a3_001_stage3_failed_retry_count_1():
    """stage3 failed + retryCount=1 → 재시도 1회 더 허용."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage3_status="failed", stage3_retry_count=1)
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        assert not result["stage3_retry_blocked"]


def test_a3_001_stage3_failed_retry_blocked():
    """stage3 failed + retryCount=2 → retry 차단."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage3_status="failed", stage3_retry_count=2)
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        assert result["stage3_retry_blocked"]
        assert any("retry" in m and "blocked" in m for m in result["messages"])


def test_a3_001_stage3_retry_blocked_preserves_output_failure():
    """A retry-blocked Stage 3 leaves output failed instead of masked completed."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(
            stage3_status="failed",
            stage3_retry_count=2,
            stage3_error="stage3 exploded",
            output_status="completed",
        )
        state["layers"]["analyze"]["stage3"]["reason"] = "reference validation failed"
        for filename in (
            "api-spec.json",
            "data-model.md",
            "domain-overview.md",
            "external-integration.md",
        ):
            (ctx.ai_context_dir / filename).write_text("current output", encoding="utf-8")
        (ctx.ai_context_dir / ".tmp" / "hashes.json").write_text(
            json.dumps({"files": [{"path": "source.py", "sha256": "current"}]}),
            encoding="utf-8",
        )
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)

        assert not result["skip_provider"]
        assert result["stage3_retry_blocked"]
        assert result["state"]["layers"]["output"]["status"] == "failed"
        assert result["state"]["layers"]["output"]["errorMessage"] == "stage3 exploded"
        assert result["state"]["completedAt"] is None


def test_a3_001_stage3_retry_blocked_resets_for_changed_sources():
    """A new source generation resets a previous Stage 3 retry budget."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(
            stage3_status="failed",
            stage3_retry_count=2,
            stage3_error="stage3 exploded",
        )
        state["layers"]["analyze"]["stage3"]["reason"] = "reference validation failed"
        stage3_file = ctx.ai_context_dir / ".tmp" / "stage3-final.md"
        stage3_file.write_text("# Failed stage3 output", encoding="utf-8")
        (ctx.ai_context_dir / ".tmp" / "hashes.json").write_text(
            json.dumps({"files": [{"path": "source.py", "sha256": "old"}]}),
            encoding="utf-8",
        )
        _save_state(ctx, state)

        result = resolve_analysis_resume(
            ctx,
            current_file_entries=[{"path": "source.py", "sha256": "new"}],
        )

        resumed_stage3 = result["state"]["layers"]["analyze"]["stage3"]
        assert not result["stage3_retry_blocked"]
        assert resumed_stage3["status"] == "pending"
        assert resumed_stage3["retryCount"] == 0
        assert resumed_stage3["errorMessage"] == "stage3 exploded"
        assert resumed_stage3["reason"] == "reference validation failed"
        assert stage3_file.exists()


def test_a3_001_stage3_retry_blocked_does_not_block_standard_mode():
    """Standard mode proceeds so Stage 3 can be explicitly skipped by policy."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp), mode="standard")
        state = _make_analysis_state(stage3_status="failed", stage3_retry_count=2)
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)

        assert not result["stage3_retry_blocked"]


def test_a3_001_stage3_failed_retry_blocked_3():
    """stage3 failed + retryCount=3 → 확실히 차단."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage3_status="failed", stage3_retry_count=3)
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        assert result["stage3_retry_blocked"]


def test_a3_001_stage3_completed_no_rerun():
    """stage3 completed → 재실행 없음 (output completed일 때 skip_provider)."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(
            stage3_status="completed",
            output_status="completed",
        )
        # Create output files so output_files_present returns True
        for fname in ("api-spec.json", "data-model.md", "domain-overview.md", "external-integration.md"):
            (ctx.ai_context_dir / fname).write_text("{}", encoding="utf-8")
        # Create hashes file at .tmp/hashes.json (dict format, non-empty)
        hashes_path = ctx.ai_context_dir / ".tmp" / "hashes.json"
        hashes_path.write_text(json.dumps({"files": [{"path": "a.ts", "hash": "abc123"}]}), encoding="utf-8")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        assert result["skip_provider"]
        assert not result["stage3_retry_blocked"]


def test_a3_001_stage3_pending_no_change():
    """stage3 pending → resume에서 변경 없음."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage3_status="pending")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        resume_state = result["state"]
        assert resume_state["layers"]["analyze"]["stage3"]["status"] == "pending"
        assert not result["stage3_retry_blocked"]


def test_a3_001_stage3_failed_preserves_diagnostics_artifact():
    """stage3 retry retains the latest failed diagnostic artifact and fields."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(
            stage3_status="failed",
            stage3_retry_count=1,
            stage3_error="stage3 exploded",
        )
        state["layers"]["analyze"]["stage3"]["reason"] = "reference validation failed"
        stage3_file = ctx.ai_context_dir / ".tmp" / "stage3-final.md"
        stage3_file.write_text("# Failed stage3 output", encoding="utf-8")
        state["artifacts"]["result_file"] = ".tmp/result.md"
        (ctx.ai_context_dir / ".tmp" / "result.md").write_text("saved stage2", encoding="utf-8")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)

        assert not result["stage3_retry_blocked"]
        assert not result["reused_result"]
        resumed_stage3 = result["state"]["layers"]["analyze"]["stage3"]
        assert resumed_stage3["status"] == "pending"
        assert resumed_stage3["errorMessage"] == "stage3 exploded"
        assert resumed_stage3["reason"] == "reference validation failed"
        assert resumed_stage3["retryCount"] == 1
        assert stage3_file.exists()


def test_a3_001_finalization_preserves_stage3_failure():
    """A successful Stage 2 finalizer cannot conceal a failed Stage 3."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(
            stage3_status="failed",
            stage3_retry_count=1,
            stage3_error="stage3 exploded",
        )
        state["layers"]["analyze"]["stage3"]["reason"] = "reference validation failed"
        for filename in (
            "api-spec.json",
            "data-model.md",
            "domain-overview.md",
            "external-integration.md",
        ):
            (ctx.ai_context_dir / filename).write_text("current output", encoding="utf-8")
        _save_state(ctx, state)

        finalized = finalize_analysis_run(ctx, "fixture", 0)

        finalized_stage3 = finalized["layers"]["analyze"]["stage3"]
        assert finalized_stage3["status"] == "failed"
        assert finalized_stage3["errorMessage"] == "stage3 exploded"
        assert finalized_stage3["reason"] == "reference validation failed"
        assert finalized_stage3["retryCount"] == 1
        assert finalized["layers"]["output"]["status"] == "failed"
        assert finalized["layers"]["output"]["errorMessage"] == "stage3 exploded"


def test_a3_001_stage3_completed_output_pending():
    """stage3=completed BUT output=pending → skip_provider=False (Stage 3 specific)."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(
            stage3_status="completed",
            output_status="pending",  # output NOT completed
        )
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        # output is pending → should NOT skip provider
        assert not result["skip_provider"]
        # stage3 is already completed → no stage3 retry issues
        assert not result["stage3_retry_blocked"]


def test_a3_001_stage2_reuse_with_stage1_complete():
    """stage1 completed + stage2 result 존재 → stage2 결과 재사용."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage2_status="completed")
        state["artifacts"]["result_file"] = ".tmp/result.md"
        (ctx.ai_context_dir / ".tmp" / "result.md").write_text("result", encoding="utf-8")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        assert result["reused_result"]




def test_a3_001_stage2_reuse_rejected_when_source_changed():
    """A saved Stage 2 result cannot cross a source-hash generation boundary."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage2_status="completed")
        state["artifacts"]["result_file"] = ".tmp/result.md"
        result_path = ctx.ai_context_dir / ".tmp" / "result.md"
        result_path.write_text("result", encoding="utf-8")
        (ctx.ai_context_dir / ".tmp" / "hashes.json").write_text(
            json.dumps({"files": [{"path": "source.py", "sha256": "old"}]}),
            encoding="utf-8",
        )
        _save_state(ctx, state)

        result = resolve_analysis_resume(
            ctx,
            current_file_entries=[{"path": "source.py", "sha256": "new"}],
        )

        assert not result["reused_result"]
        assert any("source" in message and "changed" in message for message in result["messages"])
        assert not result_path.exists()


def test_a3_001_stage2_reuse_rejected_when_config_changed():
    """A saved Stage 2 result cannot cross a bundle-config generation boundary."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage2_status="completed")
        state["layers"]["bundle"]["configHash"] = "old-config-hash"
        state["artifacts"]["domain_bundle"] = ".tmp/domain-bundle.xml"
        state["artifacts"]["result_file"] = ".tmp/result.md"
        (ctx.ai_context_dir / ".tmp" / "domain-bundle.xml").write_text(
            "old bundle",
            encoding="utf-8",
        )
        result_path = ctx.ai_context_dir / ".tmp" / "result.md"
        result_path.write_text("result", encoding="utf-8")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)

        assert not result["reused_result"]
        assert any("config" in message and "changed" in message for message in result["messages"])
        assert not result_path.exists()


def test_a3_001_stage2_reuse_blocked_without_stage1():
    """stage1 incomplete + stage2 result 존재 → stage2 결과 폐기."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _FakeContext(Path(tmp))
        state = _make_analysis_state(stage2_status="in_progress")
        state["layers"]["analyze"]["stage1"]["status"] = "pending"
        state["artifacts"]["result_file"] = ".tmp/result.md"
        (ctx.ai_context_dir / ".tmp" / "result.md").write_text("result", encoding="utf-8")
        _save_state(ctx, state)

        result = resolve_analysis_resume(ctx)
        assert not result["reused_result"]
        assert any("stage1 incomplete" in m for m in result["messages"])


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
        if "stage3_001" in name and current_group != "stage3":
            current_group = "stage3"
            print("\n--- AN-STAGE3-001: Stage 3 trigger rule ---")
        elif "a3_001" in name and current_group != "a3":
            current_group = "a3"
            print("\n--- AN-A3-001: Stage 3 resume contract ---")
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
