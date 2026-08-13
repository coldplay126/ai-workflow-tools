from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import awf.commands.analyze as analyze

from fixture_support import ANALYSIS_RESULT, ROOT, prepare_analysis_docs_fixture


class _MutationContext:
    def __init__(self, tmp_path: Path) -> None:
        self.service = "service"
        self.domain = "orders"
        self.repo_root = tmp_path
        self.docs_root = tmp_path / "docs"
        self.github_root = tmp_path / "github"
        self.ai_context_dir = self.docs_root / self.service / self.domain / ".ai-context"
        self.related_domains: list[str] = []
        self.domain_directories = ["src/orders"]
        self.all_directories = ["src/orders"]
        self.mode = "standard"
        self.ai_context_dir.mkdir(parents=True)


def _mutation_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        check=False,
        catalog=False,
        cycles=False,
        all=False,
        status=False,
        dry_run=False,
        domain="orders",
        service="service",
        repo_root=tmp_path,
        docs_root=tmp_path / "docs",
        github_root=tmp_path,
        output_format="json",
        provider="fixture",
        mode=None,
        non_interactive=True,
        print_prompt=False,
    )


def _configure_mutation_route(monkeypatch, tmp_path: Path) -> tuple[argparse.Namespace, _MutationContext]:
    args = _mutation_args(tmp_path)
    context = _MutationContext(tmp_path)
    monkeypatch.setattr(analyze, "enforce_ready_gate", lambda *args, **kwargs: 0)
    monkeypatch.setattr(analyze, "load_awf_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(analyze, "resolve_analysis_context", lambda **kwargs: context)
    monkeypatch.setattr(analyze, "_load_pipeline_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(analyze, "build_prompt", lambda *args, **kwargs: "prompt")
    return args, context


def _run_analyze(
    tmp_docs_root: Path,
    *,
    fixture_returncode: int = 0,
    output_format: str = "text",
    print_prompt: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_FIXTURE_RESULT_FILE"] = str(ANALYSIS_RESULT)
    env["AWF_FIXTURE_RETURNCODE"] = str(fixture_returncode)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "awf",
            "analyze",
            "sample-api",
            "quest-challenge",
            "--repo-root",
            str(ROOT),
            "--docs-root",
            str(tmp_docs_root),
            "--github-root",
            str(tmp_docs_root),
            "--provider",
            "fixture",
            "--yolo",
            "--no-ready-gate",
            "--output-format",
            output_format,
            *(["--print-prompt"] if print_prompt else []),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def test_provider_failure_preserves_completed_hash_baseline(tmp_path: Path) -> None:
    prepare_analysis_docs_fixture(tmp_path)

    first = _run_analyze(tmp_path)
    assert first.returncode == 0, first.stderr

    hashes_path = tmp_path / "sample-api" / "quest-challenge" / ".ai-context" / ".tmp" / "hashes.json"
    baseline = hashes_path.read_bytes()
    handler = tmp_path / "_sample-api-src" / "src" / "domain" / "quest-challenge" / "handler.py"
    handler.write_text("def start_quest(user_id: str) -> dict:\n    return {'user_id': user_id, 'status': 'failed'}\n", encoding="utf-8")

    failed = _run_analyze(tmp_path, fixture_returncode=1)

    assert failed.returncode == 1
    assert hashes_path.read_bytes() == baseline


def test_json_mode_restores_stdout_after_context_resolution_error(monkeypatch, tmp_path: Path) -> None:
    args = _mutation_args(tmp_path)
    original_stdout = sys.stdout
    monkeypatch.setattr(analyze, "enforce_ready_gate", lambda *args, **kwargs: 0)
    monkeypatch.setattr(analyze, "load_awf_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(analyze, "resolve_analysis_context", lambda **kwargs: (_ for _ in ()).throw(ValueError("context failed")))

    assert analyze.run_analyze(args) == 2
    assert sys.stdout is original_stdout


@contextmanager
def _blocked_repository_lock(*_args, **_kwargs):
    raise BlockingIOError
    yield


def test_json_mode_restores_stdout_after_lock_conflict(monkeypatch, tmp_path: Path) -> None:
    args, _ = _configure_mutation_route(monkeypatch, tmp_path)
    original_stdout = sys.stdout
    monkeypatch.setattr(analyze, "repository_lock", _blocked_repository_lock)

    assert analyze.run_analyze(args) == 4
    assert sys.stdout is original_stdout


def test_json_mode_restores_stdout_after_provider_early_failure(monkeypatch, tmp_path: Path) -> None:
    args, _ = _configure_mutation_route(monkeypatch, tmp_path)
    original_stdout = sys.stdout

    def _provider_failure(*_args, **_kwargs) -> int:
        print("error: provider failed", file=sys.stderr)
        return 1

    monkeypatch.setattr(analyze, "_run_analyze_domain_mutation", _provider_failure)

    assert analyze.run_analyze(args) == 1
    assert sys.stdout is original_stdout


def test_json_mode_emits_one_envelope_and_writes_diagnostics_to_stderr(tmp_path: Path) -> None:
    prepare_analysis_docs_fixture(tmp_path)

    completed = _run_analyze(tmp_path, output_format="json")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["command"] == "analyze"
    assert payload["status"] == "completed"
    assert "resume_mode:" not in completed.stdout
    assert "resume_mode:" in completed.stderr


def test_json_mode_print_prompt_writes_context_to_stderr(tmp_path: Path) -> None:
    prepare_analysis_docs_fixture(tmp_path)

    completed = _run_analyze(tmp_path, output_format="json", print_prompt=True)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "completed"
    assert "=== awf analyze context ===" not in completed.stdout
    assert "=== awf analyze context ===" in completed.stderr
