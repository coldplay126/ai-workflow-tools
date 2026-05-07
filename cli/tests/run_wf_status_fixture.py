from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    config_path = ROOT / ".awf.toml"
    backup = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    state_path = ROOT / ".workflow" / "state.json"
    state_backup = state_path.read_text(encoding="utf-8")
    review_report_path = ROOT / ".workflow" / "artifacts" / "review-report.md"
    review_report_backup = review_report_path.read_text(encoding="utf-8")
    config_path.write_text(
        "\n".join(
            [
                "[provider]",
                'default = "fixture"',
                "",
                "[provider.fixture]",
                'result_file = "cli/tests/fixtures/review-result.json"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "cli" / "src")
        state = json.loads((ROOT / ".workflow" / "state.json").read_text(encoding="utf-8"))
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
        (ROOT / ".workflow" / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        status = subprocess.run(
            [
                sys.executable,
                "-m",
                "awf",
                "wf",
                "status",
                "--repo-root",
                str(ROOT),
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
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

        state = json.loads((ROOT / ".workflow" / "state.json").read_text(encoding="utf-8"))
        print(f"currentPhase={state.get('currentPhase')}")
        print(f"eventSyncTasks={len((state.get('eventSync') or {}).get('tasks', {}))}")
        return 0
    finally:
        if backup is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(backup, encoding="utf-8")
        state_path.write_text(state_backup, encoding="utf-8")
        review_report_path.write_text(review_report_backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
