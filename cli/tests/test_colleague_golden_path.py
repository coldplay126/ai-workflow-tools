from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fixture_support import ROOT, REVIEW_RESULT, awf_env


def _run_awf(
    repo_root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "awf", *args],
        cwd=str(repo_root),
        env=awf_env(),
        capture_output=True,
        text=True,
    )


def _assert_success(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, (
        f"command failed with exit={completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


def _write_trial_repo(repo_root: Path) -> None:
    (repo_root / "src" / "orders").mkdir(parents=True)
    (repo_root / "src" / "inventory").mkdir(parents=True)
    (repo_root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (repo_root / "src" / "orders" / "service.py").write_text(
        "\n".join(
            [
                "def order_status(order_id: str) -> dict:",
                "    return {'order_id': order_id, 'status': 'pending'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "src" / "inventory" / "service.py").write_text(
        "\n".join(
            [
                "def reserve_sku(sku: str) -> dict:",
                "    return {'sku': sku, 'reserved': True}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / ".awf.toml").write_text(
        "\n".join(
            [
                "[provider]",
                'default = "fixture"',
                "",
                "[provider.fixture]",
                f'result_file = "{REVIEW_RESULT}"',
                "",
                "[permissions]",
                "yolo = true",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_colleague_onboarding_golden_path_is_deterministic(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "inventory-service"
    repo_root.mkdir()
    _write_trial_repo(repo_root)

    ready = _run_awf(repo_root, "ready", "--repo-root", str(repo_root))
    _assert_success(ready)
    assert "automation_level" in ready.stdout

    scan = _run_awf(repo_root, "scan", ".", "--no-ai")
    _assert_success(scan)
    scan_payload = json.loads(scan.stdout)
    assert scan_payload["service"] == "inventory-service"
    assert {unit["name"] for unit in scan_payload["units"]} >= {
        "orders",
        "inventory",
    }

    analyze = _run_awf(
        repo_root,
        "analyze",
        "inventory-service",
        "orders",
        "--repo-root",
        str(repo_root),
        "--github-root",
        str(tmp_path),
        "--dry-run",
        "--output-format",
        "json",
    )
    _assert_success(analyze)
    analyze_payload = json.loads(analyze.stdout)
    assert analyze_payload["command"] == "analyze"
    assert analyze_payload["service"] == "inventory-service"
    assert analyze_payload["domain"] == "orders"
    assert [Path(path).relative_to(repo_root).as_posix() for path in analyze_payload["domain_directories"]] == [
        "src/orders"
    ]
    assert analyze_payload["prompt"]

    init = _run_awf(
        repo_root,
        "wf",
        "init",
        "small scoped improvement",
        "--repo-root",
        str(repo_root),
    )
    _assert_success(init)
    assert (repo_root / ".workflow" / "state.json").is_file()

    gate = _run_awf(
        repo_root,
        "ready",
        "--repo-root",
        str(repo_root),
        "--gate",
        "workflow-run",
        "--json",
    )
    _assert_success(gate)
    gate_payload = json.loads(gate.stdout)
    assert gate_payload["gate"]["decision"] == "allow"

    next_dry_run = _run_awf(
        repo_root,
        "wf",
        "next",
        "--repo-root",
        str(repo_root),
        "--dry-run",
        "--output-format",
        "json",
    )
    _assert_success(next_dry_run)
    next_payload = json.loads(next_dry_run.stdout)
    assert next_payload["phase"] == "plan"
    assert next_payload["provider"] == "fixture"
    assert next_payload["prompt"]
    assert next_payload["prompt_file"] == "(dry-run, not written)"
