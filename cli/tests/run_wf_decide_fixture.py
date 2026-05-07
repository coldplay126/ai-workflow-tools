from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fixture_support import ROOT, initialize_workflow_fixture, prepare_workflow_repo, run_awf


sys.path.insert(0, str(ROOT / "cli" / "src"))

from awf.core.state import load_workflow_state


def _seed_deciding_state(repo_root: Path) -> None:
    state_path = repo_root / ".workflow" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    phases = state.setdefault("phases", {})
    review = phases.setdefault("review", {"status": "pending", "retries": 0})
    review.update(
        {
            "status": "deciding",
            "startedAt": "2026-04-01T12:00:00+09:00",
            "escapedAt": "2026-04-01T12:00:01+09:00",
            "provider": "fixture",
            "escapeReason": "scope_divergence",
            "escapeSummary": "fixture deciding state",
            "recommendedAction": "replan",
            "decision": "escalate_user",
            "decisionAt": "2026-04-01T12:00:02+09:00",
            "decisionReason": "fixture decision",
            "replanTarget": "plan",
        }
    )
    state["currentPhase"] = "review"
    state["loop"] = {
        "replanCount": 0,
        "maxReplans": 3,
        "lastEscape": {
            "phase": "review",
            "provider": "fixture",
            "reason": "scope_divergence",
            "summary": "fixture deciding state",
            "recommendedAction": "replan",
            "escapedAt": "2026-04-01T12:00:01+09:00",
        },
        "pendingDecision": {
            "phase": "review",
            "decision": "escalate_user",
            "reason": "fixture decision",
            "replanTarget": "plan",
            "recordedAt": "2026-04-01T12:00:02+09:00",
        },
        "history": [],
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_decide(repo_root: Path, *args: str) -> tuple[object, dict]:
    completed = run_awf(repo_root, "wf", "decide", *args)
    state = load_workflow_state(str(repo_root))
    return completed, state


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        temp_repo = Path(tmp_dir_str) / "repo"
        prepare_workflow_repo(temp_repo)
        initialized = initialize_workflow_fixture(
            temp_repo,
            "Fixture decide concept covering workflow decision commands",
        )
        if initialized.returncode != 0:
            print(initialized.stdout, end="")
            if initialized.stderr:
                print(initialized.stderr, file=sys.stderr, end="")
            return initialized.returncode

        _seed_deciding_state(temp_repo)
        completed, state = _run_decide(temp_repo, "continue", "--phase", "review")
        if completed.returncode != 0:
            print(completed.stdout, end="")
            print(completed.stderr, file=sys.stderr, end="")
            return completed.returncode
        review = ((state.get("phases") or {}).get("review") or {})
        if review.get("status") != "in_progress":
            raise SystemExit("continue should move review to in_progress")
        if ((state.get("loop") or {}).get("pendingDecision")) is not None:
            raise SystemExit("continue should clear pending decision")
        loop_history = (state.get("loop") or {}).get("history") or []
        if not loop_history or (loop_history[-1] or {}).get("decision") != "continue":
            raise SystemExit("continue should append loop history")

        _seed_deciding_state(temp_repo)
        completed, state = _run_decide(temp_repo, "replan", "--phase", "review", "--target", "plan")
        if completed.returncode != 0:
            print(completed.stdout, end="")
            print(completed.stderr, file=sys.stderr, end="")
            return completed.returncode
        if state.get("currentPhase") != "plan":
            raise SystemExit("replan should move currentPhase to plan")
        phases = state.get("phases") or {}
        if (phases.get("plan") or {}).get("status") != "pending":
            raise SystemExit("replan should reset target phase to pending")
        if (phases.get("review") or {}).get("status") != "pending":
            raise SystemExit("replan should reset review to pending")
        if int(((state.get("loop") or {}).get("replanCount", 0) or 0)) < 1:
            raise SystemExit("replan should increment loop.replanCount")
        loop_history = (state.get("loop") or {}).get("history") or []
        if not loop_history or (loop_history[-1] or {}).get("decision") != "replan":
            raise SystemExit("replan should append loop history")

        _seed_deciding_state(temp_repo)
        completed, state = _run_decide(temp_repo, "abort", "--phase", "review")
        if completed.returncode != 0:
            print(completed.stdout, end="")
            print(completed.stderr, file=sys.stderr, end="")
            return completed.returncode
        review = ((state.get("phases") or {}).get("review") or {})
        if review.get("status") != "aborted":
            raise SystemExit("abort should move review to aborted")
        if state.get("currentPhase") != "aborted":
            raise SystemExit("abort should move currentPhase to aborted")
        loop_history = (state.get("loop") or {}).get("history") or []
        if not loop_history or (loop_history[-1] or {}).get("decision") != "abort":
            raise SystemExit("abort should append loop history")

        print("wf_decide_ok=true")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
