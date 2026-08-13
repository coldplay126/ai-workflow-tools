from __future__ import annotations

import argparse
import multiprocessing
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import awf.commands.analyze as analyze
from awf.core import scanner
from awf.worktrees.locking import repository_lock


class _MutationContext:
    def __init__(self, tmp_path: Path):
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
        output_format="text",
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


def _hold_lock(path: str, acquired, release) -> None:
    with repository_lock(Path(path)):
        acquired.set()
        release.wait(timeout=10)


def test_all_stops_immediately_when_first_child_returns_130(monkeypatch, tmp_path: Path):
    (tmp_path / "service").mkdir()
    units = [
        SimpleNamespace(name="first", file_count=1),
        SimpleNamespace(name="second", file_count=1),
        SimpleNamespace(name="third", file_count=1),
    ]
    scan_result = SimpleNamespace(
        units=units,
        language="python",
        framework="pytest",
        unit_pattern="src/*",
    )
    child_calls = []
    delays = []
    args = _mutation_args(tmp_path)
    args.all = True
    args.delay = 10

    def _run_child(domain_args):
        child_calls.append(domain_args.domain)
        return 130

    monkeypatch.setattr(scanner, "scan_repo", lambda *args, **kwargs: scan_result)
    monkeypatch.setattr(analyze, "run_analyze", _run_child)
    monkeypatch.setattr(time, "sleep", lambda delay: delays.append(delay))

    assert analyze._run_analyze_all(args) == 130
    assert child_calls == ["first"]
    assert delays == []


def test_domain_mutation_rejects_separate_process_lock_before_provider(monkeypatch, tmp_path: Path, capsys):
    args, context = _configure_mutation_route(monkeypatch, tmp_path)
    mutation_calls = []

    def _unexpected_mutation(*args, **kwargs):
        mutation_calls.append((args, kwargs))
        raise AssertionError("mutation should not run while the analysis lock is held")

    monkeypatch.setattr(analyze, "_run_analyze_domain_mutation", _unexpected_mutation, raising=False)
    monkeypatch.setattr(analyze, "ensure_ai_context_dirs", _unexpected_mutation)
    multiprocessing_context = multiprocessing.get_context("spawn")
    acquired = multiprocessing_context.Event()
    release = multiprocessing_context.Event()
    lock_path = context.ai_context_dir / ".analysis-run.lock"
    holder = multiprocessing_context.Process(target=_hold_lock, args=(str(lock_path), acquired, release))
    holder.start()
    assert acquired.wait(timeout=10)

    try:
        assert analyze.run_analyze(args) == 4
    finally:
        release.set()
        holder.join(timeout=10)

    assert holder.exitcode == 0
    assert mutation_calls == []
    assert "analysis already running" in capsys.readouterr().err


class _HelperExploded(Exception):
    pass


def test_domain_mutation_lock_releases_when_mutation_raises(monkeypatch, tmp_path: Path):
    args, context = _configure_mutation_route(monkeypatch, tmp_path)

    def _raise_from_mutation(*args, **kwargs):
        raise _HelperExploded

    def _legacy_mutation_reached(*args, **kwargs):
        raise AssertionError("legacy mutation body was not extracted")

    monkeypatch.setattr(analyze, "_run_analyze_domain_mutation", _raise_from_mutation, raising=False)
    monkeypatch.setattr(analyze, "ensure_ai_context_dirs", _legacy_mutation_reached)

    with pytest.raises(_HelperExploded):
        analyze.run_analyze(args)

    with repository_lock(context.ai_context_dir / ".analysis-run.lock", blocking=False):
        pass


def test_status_route_ignores_analysis_mutation_lock(monkeypatch, tmp_path: Path):
    args = _mutation_args(tmp_path)
    args.status = True
    context = _MutationContext(tmp_path)
    monkeypatch.setattr(analyze, "resolve_analysis_context", lambda **kwargs: context)
    monkeypatch.setattr(analyze, "load_analysis_state", lambda *args, **kwargs: {})

    with repository_lock(context.ai_context_dir / ".analysis-run.lock"):
        assert analyze.run_analyze(args) == 0


def test_dry_run_ignores_analysis_mutation_lock(monkeypatch, tmp_path: Path):
    args, context = _configure_mutation_route(monkeypatch, tmp_path)
    args.dry_run = True
    monkeypatch.setattr(
        analyze,
        "_run_analyze_domain_mutation",
        lambda *args, **kwargs: pytest.fail("dry-run must not mutate"),
        raising=False,
    )

    with repository_lock(context.ai_context_dir / ".analysis-run.lock"):
        assert analyze.run_analyze(args) == 0


@pytest.mark.parametrize(
    ("flag", "handler_name"),
    [
        ("check", "_run_drift_check"),
        ("catalog", "_run_catalog"),
        ("cycles", "_run_cycles"),
    ],
)
def test_global_read_only_routes_ignore_analysis_mutation_lock(monkeypatch, tmp_path: Path, flag: str, handler_name: str):
    args = _mutation_args(tmp_path)
    setattr(args, flag, True)
    context = _MutationContext(tmp_path)
    monkeypatch.setattr(analyze, handler_name, lambda *args, **kwargs: 7)

    with repository_lock(context.ai_context_dir / ".analysis-run.lock"):
        assert analyze.run_analyze(args) == 7
