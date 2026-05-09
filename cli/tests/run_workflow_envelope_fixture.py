from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fixture_support import (
    ROOT,
    initialize_workflow_fixture,
    mark_workflow_prerequisites_passed,
    prepare_workflow_repo,
    run_awf,
)

sys.path.insert(0, str(ROOT / "cli" / "src"))

from awf.commands.wf import _workflow_idempotency_key
from awf.core.state import load_workflow_state
from awf.core.workflow_envelope import normalize_worker_result
from awf.core.workflow_results import load_result_envelope, load_result_json


def _run_wf_with_fixture(
    result_file: Path,
    *,
    returncode: int = 0,
    loop: dict | None = None,
) -> tuple[object, dict]:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        temp_repo = Path(tmp_dir_str) / "repo"
        prepare_workflow_repo(temp_repo, result_file=result_file)
        initialized = initialize_workflow_fixture(
            temp_repo,
            "Fixture envelope concept covering escaped and completed worker envelopes",
        )
        if initialized.returncode != 0:
            print(initialized.stdout, end="")
            if initialized.stderr:
                print(initialized.stderr, file=sys.stderr, end="")
            return initialized, {}
        mark_workflow_prerequisites_passed(temp_repo)
        state_path = temp_repo / ".workflow" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if loop is not None:
            state["loop"] = loop
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        completed = run_awf(
            temp_repo,
            "wf",
            "next",
            "--phase",
            "review",
            "--mode",
            "solo",
            "--auto-apply",
            extra_env={
                "AWF_FIXTURE_RESULT_FILE": str(result_file),
                "AWF_FIXTURE_RETURNCODE": str(returncode),
            },
        )
        state_snapshot = load_workflow_state(str(temp_repo))
        return completed, state_snapshot


def main() -> int:
    legacy_path = ROOT / "cli" / "tests" / "fixtures" / "review-result.json"
    completed_path = ROOT / "cli" / "tests" / "fixtures" / "review-result-envelope-completed.json"
    escaped_path = ROOT / "cli" / "tests" / "fixtures" / "review-result-envelope-escaped.json"

    legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    wrapped = normalize_worker_result(legacy_payload, phase="review", provider="fixture")
    assert wrapped["status"] == "completed"
    assert wrapped["meta"]["wrapped_legacy_result"] is True
    assert wrapped["result"]["conclusion"].startswith("PASS")

    completed_payload = load_result_envelope(str(completed_path), phase="review", provider="fixture")
    assert completed_payload["status"] == "completed"
    assert completed_payload["result"]["coverage"]["percentage"] == 100

    completed_bare = load_result_json(str(completed_path))
    assert completed_bare["coverage"]["percentage"] == 100

    escaped_payload = load_result_envelope(str(escaped_path), phase="review", provider="fixture")
    assert escaped_payload["status"] == "escaped"
    assert escaped_payload["escape"]["recommended_action"] == "replan"

    idempotency_key = _workflow_idempotency_key({"id": "wf-x", "loop": {"replanCount": 2}}, "review")
    assert idempotency_key == "wf-x:review:2"

    escaped_run, escaped_state = _run_wf_with_fixture(escaped_path)
    print(escaped_run.stdout, end="")
    if escaped_run.stderr:
        print(escaped_run.stderr, file=sys.stderr, end="")
    if escaped_run.returncode == 0:
        raise SystemExit("escaped envelope unexpectedly passed")
    if "worker_status: escaped" not in escaped_run.stderr:
        raise SystemExit("missing worker_status escaped")
    if "escape_reason: scope_divergence" not in escaped_run.stderr:
        raise SystemExit("missing escape_reason")
    review_state = ((escaped_state.get("phases") or {}).get("review") or {})
    if review_state.get("status") != "pending":
        raise SystemExit("scope divergence should auto-replan review back to pending")
    if escaped_state.get("currentPhase") != "plan":
        raise SystemExit("scope divergence should move currentPhase to plan")
    if int((((escaped_state.get("loop") or {}).get("replanCount", 0) or 0))) < 1:
        raise SystemExit("auto replan should increment loop.replanCount")
    event_sync = escaped_state.get("eventSync") or {}
    if ((event_sync.get("escapes") or {}).get("review") or {}).get("reason") != "scope_divergence":
        raise SystemExit("missing escape sync")
    if ((event_sync.get("decisions") or {}).get("review") or {}).get("decision") != "replan":
        raise SystemExit("missing decision sync")

    escaped_nonzero_run, escaped_nonzero_state = _run_wf_with_fixture(escaped_path, returncode=7)
    if escaped_nonzero_run.returncode == 0:
        raise SystemExit("escaped envelope with nonzero rc unexpectedly passed")
    if "worker_status: escaped" not in (escaped_nonzero_run.stderr or ""):
        raise SystemExit("missing escaped status for nonzero rc")
    if escaped_nonzero_state.get("currentPhase") != "plan":
        raise SystemExit("nonzero escaped workflow should still auto-replan")

    escaped_budget_run, escaped_budget_state = _run_wf_with_fixture(
        escaped_path,
        loop={"replanCount": 3, "maxReplans": 3},
    )
    if escaped_budget_run.returncode == 0:
        raise SystemExit("budget-exhausted escaped workflow unexpectedly passed")
    review_state = ((escaped_budget_state.get("phases") or {}).get("review") or {})
    if review_state.get("status") != "deciding":
        raise SystemExit("budget exhaustion should leave phase in deciding")
    if review_state.get("decision") != "escalate_user":
        raise SystemExit("budget exhaustion should escalate user")

    with tempfile.TemporaryDirectory() as tmp:
        advisory_path = Path(tmp) / "review-result-envelope-advisory.json"
        advisory_path.write_text(
            json.dumps(
                {
                    "status": "escaped",
                    "phase": "review",
                    "provider": "fixture",
                    "result": {},
                    "escape": {
                        "severity": "advisory",
                        "reason": "quality_threshold",
                        "summary": "minor quality drift",
                        "recommended_action": "continue",
                    },
                    "meta": {"format_version": 1},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        advisory_run, advisory_state = _run_wf_with_fixture(advisory_path)
        if advisory_run.returncode == 0:
            raise SystemExit("advisory escaped workflow unexpectedly passed inline")
        review_state = ((advisory_state.get("phases") or {}).get("review") or {})
        if review_state.get("status") != "in_progress":
            raise SystemExit("advisory escape should auto-continue the phase")
        if ((advisory_state.get("loop") or {}).get("pendingDecision")) is not None:
            raise SystemExit("auto-continue should clear pending decision")

    with tempfile.TemporaryDirectory() as tmp:
        abort_path = Path(tmp) / "review-result-envelope-abort.json"
        abort_path.write_text(
            json.dumps(
                {
                    "status": "escaped",
                    "phase": "review",
                    "provider": "fixture",
                    "result": {},
                    "escape": {
                        "severity": "blocking",
                        "reason": "constraint_violation",
                        "summary": "planned change exceeds workflow constraints",
                        "recommended_action": "abort",
                    },
                    "meta": {"format_version": 1},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        abort_run, abort_state = _run_wf_with_fixture(abort_path)
        if abort_run.returncode == 0:
            raise SystemExit("constraint violation unexpectedly passed")
        review_state = ((abort_state.get("phases") or {}).get("review") or {})
        if review_state.get("status") != "aborted":
            raise SystemExit("constraint violation should auto-abort")
        if abort_state.get("currentPhase") != "aborted":
            raise SystemExit("constraint violation should move workflow to aborted")

    completed_run, _ = _run_wf_with_fixture(completed_path)
    if completed_run.returncode != 0:
        print(completed_run.stdout, end="")
        if completed_run.stderr:
            print(completed_run.stderr, file=sys.stderr, end="")
        return completed_run.returncode
    print("workflow_envelope_ok=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
