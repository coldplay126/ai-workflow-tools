from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli" / "src"))

from awf.core.state import load_workflow_state


def _seed_deciding_state() -> None:
    state_path = ROOT / ".workflow" / "state.json"
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


def _run_decide(*args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "awf", "wf", "decide", *args, "--repo-root", str(ROOT)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    state = load_workflow_state(str(ROOT))
    return completed, state


def main() -> int:
    state_path = ROOT / ".workflow" / "state.json"
    state_backup = state_path.read_text(encoding="utf-8")
    try:
        _seed_deciding_state()
        completed, state = _run_decide("continue", "--phase", "review")
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

        _seed_deciding_state()
        completed, state = _run_decide("replan", "--phase", "review", "--target", "plan")
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

        _seed_deciding_state()
        completed, state = _run_decide("abort", "--phase", "review")
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
    finally:
        state_path.write_text(state_backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
