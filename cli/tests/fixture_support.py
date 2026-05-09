from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REVIEW_RESULT = ROOT / "cli" / "tests" / "fixtures" / "review-result.json"
VERIFY_RESULT = ROOT / "cli" / "tests" / "fixtures" / "verify-result.json"
ANALYSIS_RESULT = ROOT / "cli" / "tests" / "fixtures" / "analysis-stage2-result.txt"
WF_TEMPLATE_ROOT = ROOT / "claude" / "skills" / "wf-orchestrator" / "templates"


def awf_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_SKILLS_DIR"] = str(ROOT / "claude" / "skills")
    env["AWF_WORKFLOW_TEMPLATE_DIR"] = str(WF_TEMPLATE_ROOT)
    if extra:
        env.update(extra)
    return env


def run_awf(
    repo_root: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "awf", *args, "--repo-root", str(repo_root)],
        cwd=str(repo_root),
        env=awf_env(extra_env),
        capture_output=True,
        text=True,
    )


def write_fixture_project_config(
    repo_root: Path,
    *,
    result_file: Path | None = REVIEW_RESULT,
    session_db: Path | None = None,
    yolo: bool = True,
    analysis_lines: list[str] | None = None,
) -> None:
    lines = [
        "[provider]",
        'default = "fixture"',
        "",
        "[provider.fixture]",
        f'result_file = "{str(result_file) if result_file is not None else ""}"',
        "",
    ]
    if session_db is not None:
        lines.extend(
            [
                "[paths]",
                f'session_db = "{session_db}"',
                "",
            ]
        )
    if yolo:
        lines.extend(["[permissions]", "yolo = true", ""])
    if analysis_lines:
        lines.extend(["[analysis]", *analysis_lines, ""])
    (repo_root / ".awf.toml").write_text("\n".join(lines), encoding="utf-8")


def seed_workflow_templates(repo_root: Path) -> None:
    wf_dir = repo_root / ".workflow"
    (wf_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (wf_dir / "tmp").mkdir(parents=True, exist_ok=True)
    shutil.copytree(WF_TEMPLATE_ROOT / "agent-cards", wf_dir / "agent-cards", dirs_exist_ok=True)
    shutil.copy2(WF_TEMPLATE_ROOT / "provider-config.default.json", wf_dir / "provider-config.json")


def write_minimal_workflow_artifacts(repo_root: Path) -> None:
    artifacts = repo_root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "spec.md").write_text("# Spec\n\n- FR-001: Fixture requirement\n", encoding="utf-8")
    (artifacts / "plan.md").write_text("# Plan\n\n- Implement fixture requirement [FR-001]\n", encoding="utf-8")
    (artifacts / "tasks.md").write_text("# Tasks\n\n- [X] T001 Fixture task [FR-001]\n", encoding="utf-8")
    (artifacts / "test-criteria.md").write_text("# Test Criteria\n\n- ATC-001 [FR-001]\n", encoding="utf-8")
    (artifacts / "allowed-files.json").write_text(
        json.dumps({"planned_files": ["docs/awf-cli-architecture.md"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (artifacts / "impl-log.md").write_text("# Implementation Log\n\n- Fixture implementation complete\n", encoding="utf-8")
    (artifacts / "review-report.md").write_text("# Review Report\n\nPASS - fixture review\n", encoding="utf-8")
    (artifacts / "verification-report.md").write_text("# Verification Report\n\nPASS - fixture verify\n", encoding="utf-8")


def prepare_workflow_repo(repo_root: Path, *, result_file: Path | None = REVIEW_RESULT) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        ROOT / "docs" / "architecture" / "awf-cli-architecture.md",
        repo_root / "docs" / "awf-cli-architecture.md",
    )
    seed_workflow_templates(repo_root)
    write_minimal_workflow_artifacts(repo_root)
    write_fixture_project_config(repo_root, result_file=result_file)


def initialize_workflow_fixture(repo_root: Path, concept: str) -> subprocess.CompletedProcess[str]:
    return run_awf(repo_root, "wf", "init", concept)


def mark_workflow_prerequisites_passed(repo_root: Path) -> None:
    state_path = repo_root / ".workflow" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["gates"]["G1"] = {"passed": True, "provider": "fixture", "provider_status": "completed"}
    state["gates"]["G4"] = {"passed": True, "provider": "fixture", "provider_status": "completed"}
    state["phases"]["plan"]["status"] = "completed"
    state["phases"]["impl"]["status"] = "completed"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_analysis_docs_fixture(docs_root: Path, *, service_root: Path | None = None) -> Path:
    service_root = service_root or (docs_root / "_sample-api-src")
    quest_dir = service_root / "src" / "domain" / "quest-challenge"
    extra_dir = service_root / "src" / "domain" / "quest-extra-fixture"
    health_dir = service_root / "src" / "health"
    quest_dir.mkdir(parents=True, exist_ok=True)
    extra_dir.mkdir(parents=True, exist_ok=True)
    health_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "handler.py").write_text(
        "\n".join(
            [
                "def start_quest(user_id: str) -> dict:",
                "    return {'user_id': user_id, 'status': 'started'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (quest_dir / "model.py").write_text(
        "\n".join(
            [
                "QUEST_STATES = ['started', 'completed']",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (extra_dir / "extra.py").write_text("EXTRA_FIXTURE = True\n", encoding="utf-8")
    (health_dir / "health.py").write_text(
        "\n".join(
            [
                "def health_check() -> str:",
                "    return 'ok'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    templates_dir = docs_root / "_templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    analysis_config = {
        "service_map": {"sample-api": str(service_root)},
        "include_patterns": ["**/*.py", "**/*.md", "**/*.json"],
        "domain_definitions": {
            "quest-challenge": {
                "directories": {"sample-api": ["src/domain/quest-challenge"]},
                "related_domains": [],
                "existing_docs": [],
            },
            "health": {
                "directories": {"sample-api": ["src/health"]},
                "related_domains": [],
                "existing_docs": [],
            },
        },
    }
    (templates_dir / "analysis-config.json").write_text(
        json.dumps(analysis_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (templates_dir / "analysis-pipeline.json").write_text(
        json.dumps(
            {
                "stage_routing": {
                    "small": {"stage1": "fixture", "stage2": "fixture", "stage3": "skip"},
                    "standard": {"stage1": "fixture", "stage2": "fixture", "stage3": "skip"},
                    "large": {"stage1": "fixture", "stage2": "fixture", "stage3": "skip"},
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return service_root
