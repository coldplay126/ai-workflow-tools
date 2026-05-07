from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fixture_support import initialize_workflow_fixture, prepare_workflow_repo, run_awf


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        temp_repo = Path(tmp_dir_str) / "repo"
        prepare_workflow_repo(temp_repo)
        initialized = initialize_workflow_fixture(
            temp_repo,
            "Fixture status concept covering workflow status summary",
        )
        if initialized.returncode != 0:
            print(initialized.stdout, end="")
            if initialized.stderr:
                print(initialized.stderr, file=sys.stderr, end="")
            return initialized.returncode
        state_path = temp_repo / ".workflow" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["eventSync"] = {
            "lastEventAt": "2026-03-31T18:55:00+09:00",
            "stages": {
                "prepare": {"status": "completed", "durationSec": 0.1},
                "execute": {"status": "completed", "durationSec": 0.4},
                "apply": {"status": "completed", "durationSec": 0.2},
            },
            "phases": {"review": {"status": "completed", "durationSec": 0.4}},
            "tasks": {"wf-review-1": {"type": "wf_phase", "status": "completed", "provider": "fixture"}},
            "escapes": {
                "review": {
                    "reason": "scope_divergence",
                    "summary": "fixture escape",
                    "provider": "fixture",
                    "recommendedAction": "replan",
                }
            },
            "decisions": {
                "review": {
                    "decision": "escalate_user",
                    "reason": "scope_divergence requires replan decision",
                    "replanTarget": "plan",
                }
            },
            "gates": {"G2": {"passed": True, "recordedAt": "2026-03-31T18:55:00+09:00"}},
            "artifacts": {"created": 3, "updated": 0, "byKind": {"wf_prompt": 1, "wf_result": 1, "wf_artifact": 1}},
        }
        state["loop"] = {
            "replanCount": 1,
            "maxReplans": 3,
            "lastEscape": {
                "phase": "review",
                "provider": "fixture",
                "reason": "scope_divergence",
                "summary": "fixture escape",
                "recommendedAction": "replan",
            },
            "pendingDecision": {
                "phase": "review",
                "decision": "escalate_user",
                "reason": "scope_divergence requires replan decision",
                "replanTarget": "plan",
            },
            "history": [
                {
                    "fromPhase": "impl",
                    "toPhase": "plan",
                    "decision": "replan",
                    "reason": "dependency_missing",
                    "at": "2026-03-30T18:50:00+09:00",
                },
                {
                    "fromPhase": "review",
                    "toPhase": None,
                    "decision": "continue",
                    "reason": "minor ambiguity accepted",
                    "at": "2026-03-31T18:00:00+09:00",
                },
                {
                    "fromPhase": "review",
                    "toPhase": "plan",
                    "decision": "replan",
                    "reason": "scope_divergence",
                    "at": "2026-03-31T18:55:00+09:00",
                },
            ],
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        status = run_awf(temp_repo, "wf", "status")
        print(status.stdout, end="")
        if status.stderr:
            print(status.stderr, file=sys.stderr, end="")
        if status.returncode != 0:
            return status.returncode

        if "event_summary:" not in status.stdout:
            raise SystemExit("missing event_summary")
        if "event_stages:" not in status.stdout:
            raise SystemExit("missing event_stages")
        if "event_phases:" not in status.stdout:
            raise SystemExit("missing event_phases")
        if "event_tasks:" not in status.stdout:
            raise SystemExit("missing event_tasks")
        if "event_gates:" not in status.stdout:
            raise SystemExit("missing event_gates")
        if "event_artifacts:" not in status.stdout:
            raise SystemExit("missing event_artifacts")
        if "event_escapes:" not in status.stdout:
            raise SystemExit("missing event_escapes")
        if "event_decisions:" not in status.stdout:
            raise SystemExit("missing event_decisions")
        if "last_escape:" not in status.stdout:
            raise SystemExit("missing last_escape")
        if "pending_decision:" not in status.stdout:
            raise SystemExit("missing pending_decision")
        if "loop_budget:" not in status.stdout:
            raise SystemExit("missing loop_budget")
        if "loop_history:" not in status.stdout:
            raise SystemExit("missing loop_history")

        state = json.loads(state_path.read_text(encoding="utf-8"))
        print(f"currentPhase={state.get('currentPhase')}")
        print(f"eventSyncTasks={len((state.get('eventSync') or {}).get('tasks', {}))}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
