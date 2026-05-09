from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fixture_support import (
    ANALYSIS_RESULT,
    REVIEW_RESULT,
    ROOT,
    initialize_workflow_fixture,
    mark_workflow_prerequisites_passed,
    prepare_analysis_docs_fixture,
    prepare_workflow_repo,
    write_fixture_project_config,
)


def _prepare_ready_scan_fixture(repo_root: Path) -> None:
    (repo_root / "pyproject.toml").write_text("[project]\nname = 'router-fixture'\n", encoding="utf-8")
    unit_dir = repo_root / "src" / "orders"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "service.py").write_text(
        "def handle_order():\n    return {'ok': True}\n",
        encoding="utf-8",
    )


def _disable_fixture_dual_strategy(repo_root: Path) -> None:
    provider_config_path = repo_root / ".workflow" / "provider-config.json"
    provider_config = json.loads(provider_config_path.read_text(encoding="utf-8"))
    provider_config.setdefault("wf", {})["dual_strategy_phases"] = []
    provider_config_path.write_text(
        json.dumps(provider_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_awf(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_SKILLS_DIR"] = str(ROOT / "claude" / "skills")
    env["AWF_SESSION_DB"] = str(_run_awf.db_path)
    env["AWF_DOCS_ROOT"] = str(_run_awf.docs_root)
    env["AWF_FIXTURE_RESULT_FILE"] = str(REVIEW_RESULT)
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-m", "awf", *args]
    return subprocess.run(cmd, cwd=str(_run_awf.repo_root), env=env, capture_output=True, text=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        temp_repo = tmp_dir / "repo"
        tmp_docs_root = tmp_dir / "docs"
        db_path = tmp_dir / "awf.db"
        _run_awf.repo_root = temp_repo
        _run_awf.docs_root = tmp_docs_root
        _run_awf.db_path = db_path
        prepare_workflow_repo(temp_repo, result_file=REVIEW_RESULT)
        _prepare_ready_scan_fixture(temp_repo)
        write_fixture_project_config(temp_repo, result_file=REVIEW_RESULT, session_db=db_path)
        prepare_analysis_docs_fixture(tmp_docs_root)
        initialized = initialize_workflow_fixture(
            temp_repo,
            "Fixture router workflow concept covering review and verify gates",
        )
        if initialized.returncode != 0:
            print(initialized.stdout, end="")
            if initialized.stderr:
                print(initialized.stderr, file=sys.stderr, end="")
            return initialized.returncode
        _disable_fixture_dual_strategy(temp_repo)
        mark_workflow_prerequisites_passed(temp_repo)

        routed_status = _run_awf("workflow", "status", "보여줘")
        print(routed_status.stdout, end="")
        if routed_status.stderr:
            print(routed_status.stderr, file=sys.stderr, end="")
        if routed_status.returncode != 0:
            return routed_status.returncode
        if "current_phase:" not in routed_status.stdout and "workflow state not found" not in routed_status.stderr:
            return 1

        routed_doctor = _run_awf("provider", "상태", "확인해줘")
        print(routed_doctor.stdout, end="")
        if routed_doctor.stderr:
            print(routed_doctor.stderr, file=sys.stderr, end="")
        if routed_doctor.returncode != 0:
            return routed_doctor.returncode
        if "default_provider:" not in routed_doctor.stdout:
            return 1
        if "providers:" not in routed_doctor.stdout:
            return 1

        routed_doctor_probe = _run_awf("provider", "상태", "probe", "확인해줘")
        print(routed_doctor_probe.stdout, end="")
        if routed_doctor_probe.stderr:
            print(routed_doctor_probe.stderr, file=sys.stderr, end="")
        if routed_doctor_probe.returncode != 0:
            return routed_doctor_probe.returncode
        if "probe:" not in routed_doctor_probe.stdout:
            return 1

        routed_chat = _run_awf("간단히", "도와줘")
        print(routed_chat.stdout, end="")
        if routed_chat.stderr:
            print(routed_chat.stderr, file=sys.stderr, end="")
        if routed_chat.returncode != 0:
            return routed_chat.returncode
        if "session_id:" not in routed_chat.stdout:
            return 1
        if "PASS - review fixture" not in routed_chat.stdout:
            return 1

        routed_session_list = _run_awf("세션", "목록", "보여줘")
        print(routed_session_list.stdout, end="")
        if routed_session_list.stderr:
            print(routed_session_list.stderr, file=sys.stderr, end="")
        if routed_session_list.returncode != 0:
            return routed_session_list.returncode
        if "session_db:" not in routed_session_list.stdout:
            return 1

        routed_show_latest = _run_awf("최근", "세션", "보여줘")
        print(routed_show_latest.stdout, end="")
        if routed_show_latest.stderr:
            print(routed_show_latest.stderr, file=sys.stderr, end="")
        if routed_show_latest.returncode != 0:
            return routed_show_latest.returncode
        if "session_id:" not in routed_show_latest.stdout:
            return 1
        if "assistant" not in routed_show_latest.stdout:
            return 1

        routed_compact_latest = _run_awf("최근", "세션", "압축해줘")
        print(routed_compact_latest.stdout, end="")
        if routed_compact_latest.stderr:
            print(routed_compact_latest.stderr, file=sys.stderr, end="")
        if routed_compact_latest.returncode != 0:
            return routed_compact_latest.returncode
        if "compacted:" not in routed_compact_latest.stdout:
            return 1

        routed_analyze = _run_awf("sample-api", "quest-challenge", "분석해줘")
        print(routed_analyze.stdout, end="")
        if routed_analyze.stderr:
            print(routed_analyze.stderr, file=sys.stderr, end="")
        if routed_analyze.returncode != 0:
            return routed_analyze.returncode
        if "=== awf analyze context ===" not in routed_analyze.stdout:
            return 1
        if "Analyze the `quest-challenge` unit for service `sample-api`" not in routed_analyze.stdout:
            return 1

        routed_analyze_inferred = _run_awf("quest", "challenge", "분석해줘")
        print(routed_analyze_inferred.stdout, end="")
        if routed_analyze_inferred.stderr:
            print(routed_analyze_inferred.stderr, file=sys.stderr, end="")
        if routed_analyze_inferred.returncode != 0:
            return routed_analyze_inferred.returncode
        if "Analyze the `quest-challenge` unit for service `sample-api`" not in routed_analyze_inferred.stdout:
            return 1

        routed_health_inferred = _run_awf("health", "분석해줘")
        print(routed_health_inferred.stdout, end="")
        if routed_health_inferred.stderr:
            print(routed_health_inferred.stderr, file=sys.stderr, end="")
        if routed_health_inferred.returncode != 0:
            return routed_health_inferred.returncode
        if "Analyze the `health` unit for service `sample-api`" not in routed_health_inferred.stdout:
            return 1

        routed_health_fuzzy = _run_awf("healt", "analyss")
        print(routed_health_fuzzy.stdout, end="")
        if routed_health_fuzzy.stderr:
            print(routed_health_fuzzy.stderr, file=sys.stderr, end="")
        if routed_health_fuzzy.returncode != 0:
            return routed_health_fuzzy.returncode
        if "Analyze the `health` unit for service `sample-api`" not in routed_health_fuzzy.stdout:
            return 1

        routed_analyze_status = _run_awf(
            "quest",
            "challenge",
            "분석",
            "상태",
            "보여줘",
            extra_env={
                "AWF_DOCS_ROOT": str(tmp_docs_root),
                "AWF_FIXTURE_RESULT_FILE": str(ANALYSIS_RESULT),
            },
        )
        print(routed_analyze_status.stdout, end="")
        if routed_analyze_status.stderr:
            print(routed_analyze_status.stderr, file=sys.stderr, end="")
        if routed_analyze_status.returncode != 0:
            return routed_analyze_status.returncode
        if "output_status:" not in routed_analyze_status.stdout:
            return 1
        if "current_stage:" not in routed_analyze_status.stdout:
            return 1

        routed_analyze_run = _run_awf(
            "sample-api",
            "quest-challenge",
            "분석",
            "실행",
            extra_env={
                "AWF_DOCS_ROOT": str(tmp_docs_root),
                "AWF_FIXTURE_RESULT_FILE": str(ANALYSIS_RESULT),
            },
        )
        print(routed_analyze_run.stdout, end="")
        if routed_analyze_run.stderr:
            print(routed_analyze_run.stderr, file=sys.stderr, end="")
        if routed_analyze_run.returncode != 0:
            return routed_analyze_run.returncode
        analysis_state_path = tmp_docs_root / "sample-api" / "quest-challenge" / ".ai-context" / ".analysis-state.json"
        if not analysis_state_path.exists():
            return 1
        analysis_state = json.loads(analysis_state_path.read_text(encoding="utf-8"))
        if analysis_state["layers"]["output"]["status"] != "completed":
            return 1

        routed_analyze_inferred_run = _run_awf(
            "quest",
            "challenge",
            "분석",
            "실행",
            extra_env={
                "AWF_DOCS_ROOT": str(tmp_docs_root),
                "AWF_FIXTURE_RESULT_FILE": str(ANALYSIS_RESULT),
            },
        )
        print(routed_analyze_inferred_run.stdout, end="")
        if routed_analyze_inferred_run.stderr:
            print(routed_analyze_inferred_run.stderr, file=sys.stderr, end="")
        if routed_analyze_inferred_run.returncode != 0:
            return routed_analyze_inferred_run.returncode

        routed_review = _run_awf("review", "해줘")
        print(routed_review.stdout, end="")
        if routed_review.stderr:
            print(routed_review.stderr, file=sys.stderr, end="")
        if routed_review.returncode != 0:
            return routed_review.returncode
        if "=== awf wf next ===" not in routed_review.stdout:
            return 1
        if "phase: review" not in routed_review.stdout:
            return 1

        routed_review_run = _run_awf("review", "실행")
        print(routed_review_run.stdout, end="")
        if routed_review_run.stderr:
            print(routed_review_run.stderr, file=sys.stderr, end="")
        if routed_review_run.returncode != 0:
            return routed_review_run.returncode
        review_run_output = routed_review_run.stdout + routed_review_run.stderr
        if "prompt_file:" not in review_run_output:
            return 1
        if "provider: fixture" not in review_run_output:
            return 1
        if "result:" not in review_run_output:
            return 1

        routed_verify = _run_awf("verify", "해줘")
        print(routed_verify.stdout, end="")
        if routed_verify.stderr:
            print(routed_verify.stderr, file=sys.stderr, end="")
        if routed_verify.returncode != 0:
            return routed_verify.returncode
        if "=== awf wf next ===" not in routed_verify.stdout:
            return 1
        if "phase: verify" not in routed_verify.stdout:
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
