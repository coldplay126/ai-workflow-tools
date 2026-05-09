"""Tests for ``awf wiki`` CLI handlers — init, decision (incl. --from-pr)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.commands.wiki import (
    run_wiki_decision,
    run_wiki_init,
    run_wiki_lint,
    run_wiki_log,
    run_wiki_regenerate_index,
)
from awf.core.wiki import (
    PROFILE_CONSUMER,
    PROFILE_SELF_IMPROVEMENT,
    decision_path,
    read_page,
    read_profile,
    starter_directories,
    wiki_root,
)


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_init_creates_starter_dirs_for_consumer(tmp_path, capsys):
    args = _ns(repo_root=str(tmp_path), profile=None)
    assert run_wiki_init(args) == 0
    for sub in starter_directories(PROFILE_CONSUMER):
        assert (wiki_root(tmp_path) / sub).is_dir()
    assert read_profile(tmp_path) == PROFILE_CONSUMER


def test_init_with_self_improvement_writes_profile(tmp_path):
    args = _ns(repo_root=str(tmp_path), profile=PROFILE_SELF_IMPROVEMENT)
    assert run_wiki_init(args) == 0
    assert read_profile(tmp_path) == PROFILE_SELF_IMPROVEMENT
    # self_improvement adds concepts/, not services/
    assert (wiki_root(tmp_path) / "concepts").is_dir()
    assert not (wiki_root(tmp_path) / "services").exists()


def test_init_idempotent_can_run_twice(tmp_path):
    args = _ns(repo_root=str(tmp_path), profile=PROFILE_CONSUMER)
    assert run_wiki_init(args) == 0
    assert run_wiki_init(args) == 0  # second call must not fail
    assert read_profile(tmp_path) == PROFILE_CONSUMER


def test_init_prints_self_improvement_hint_for_awf_repo(tmp_path, capsys):
    # Mimic awf-cli repo layout: cli/pyproject.toml with name = "awf-cli".
    pyproject = tmp_path / "cli" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text('name = "awf-cli"\n', encoding="utf-8")

    args = _ns(repo_root=str(tmp_path), profile=None)
    run_wiki_init(args)
    err = capsys.readouterr().err
    assert "self_improvement" in err  # hint printed


def test_init_no_hint_for_arbitrary_repo(tmp_path, capsys):
    args = _ns(repo_root=str(tmp_path), profile=None)
    run_wiki_init(args)
    err = capsys.readouterr().err
    assert "self_improvement" not in err


# --------------------------------------------------------------------------
# decision
# --------------------------------------------------------------------------


def test_decision_creates_page_with_default_consumer_profile(tmp_path):
    args = _ns(
        repo_root=str(tmp_path),
        title="Tune scope policy",
        profile=None,
        from_pr=None,
        force=False,
    )
    assert run_wiki_decision(args) == 0
    target = decision_path(tmp_path, title="Tune scope policy")
    assert target.exists()
    page = read_page(target)
    assert page.frontmatter["title"] == "Tune scope policy"
    # Consumer template phrasing
    assert "Configuration / policy chosen" in page.body


def test_decision_uses_recorded_profile(tmp_path):
    # Init sets self_improvement; decision picks it up automatically.
    run_wiki_init(_ns(repo_root=str(tmp_path), profile=PROFILE_SELF_IMPROVEMENT))
    args = _ns(
        repo_root=str(tmp_path),
        title="run_chained interface",
        profile=None,
        from_pr=None,
        force=False,
    )
    assert run_wiki_decision(args) == 0
    target = decision_path(tmp_path, title="run_chained interface")
    page = read_page(target)
    assert "Options considered" in page.body  # self_improvement template


def test_decision_explicit_profile_overrides_recorded(tmp_path):
    run_wiki_init(_ns(repo_root=str(tmp_path), profile=PROFILE_SELF_IMPROVEMENT))
    args = _ns(
        repo_root=str(tmp_path),
        title="Tune analysis",
        profile=PROFILE_CONSUMER,
        from_pr=None,
        force=False,
    )
    assert run_wiki_decision(args) == 0
    target = decision_path(tmp_path, title="Tune analysis")
    page = read_page(target)
    assert "Configuration / policy chosen" in page.body  # consumer phrasing


def test_decision_refuses_to_overwrite_without_force(tmp_path, capsys):
    args = _ns(
        repo_root=str(tmp_path),
        title="duplicate",
        profile=None,
        from_pr=None,
        force=False,
    )
    assert run_wiki_decision(args) == 0
    # Second call without --force must fail with non-zero.
    assert run_wiki_decision(args) == 2
    err = capsys.readouterr().err
    assert "already exists" in err


def test_decision_force_overwrites(tmp_path):
    args1 = _ns(
        repo_root=str(tmp_path),
        title="topic",
        profile=None,
        from_pr=None,
        force=False,
    )
    assert run_wiki_decision(args1) == 0
    args2 = _ns(
        repo_root=str(tmp_path),
        title="topic",
        profile=None,
        from_pr=None,
        force=True,
    )
    assert run_wiki_decision(args2) == 0  # second call succeeds


# --------------------------------------------------------------------------
# decision --from-pr (mocked gh)
# --------------------------------------------------------------------------


def _mock_gh_run(stdout: str, returncode: int = 0):
    """Build a subprocess.run replacement that returns canned output."""
    class _Result:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode
    def _runner(*args, **kwargs):
        return _Result()
    return _runner


def test_decision_from_pr_prefills_context_and_body(tmp_path):
    canned = json.dumps({
        "number": 26,
        "title": "feat(core): wire CmuxDispatch backend",
        "body": "## Summary\n- Stdlib-only bridge to cmux-agent\n",
        "url": "https://example.com/pr/26",
    })
    with patch("awf.commands.wiki.subprocess.run", side_effect=_mock_gh_run(canned)):
        args = _ns(
            repo_root=str(tmp_path),
            title="cmux dispatch bridge",
            profile=None,
            from_pr=26,
            force=False,
        )
        assert run_wiki_decision(args) == 0

    target = decision_path(tmp_path, title="cmux dispatch bridge")
    page = read_page(target)
    assert page.frontmatter["context_prs"] == ["#26"]
    assert "Source PR excerpt" in page.body
    assert "Stdlib-only bridge" in page.body


def test_decision_from_pr_falls_back_when_gh_fails(tmp_path, capsys):
    with patch("awf.commands.wiki.subprocess.run", side_effect=FileNotFoundError("gh missing")):
        args = _ns(
            repo_root=str(tmp_path),
            title="manual title",
            profile=None,
            from_pr=99,
            force=False,
        )
        # Even when gh is missing, we still create the decision page —
        # telemetry/metadata fetch failure must not block authoring.
        assert run_wiki_decision(args) == 0

    target = decision_path(tmp_path, title="manual title")
    page = read_page(target)
    # Best-effort fallback records the requested PR ref.
    assert page.frontmatter["context_prs"] == ["#99"]
    err = capsys.readouterr().err
    assert "gh pr view failed" in err


# --------------------------------------------------------------------------
# log + regenerate-index + lint smoke (already covered structurally; here
# we exercise the CLI handlers end-to-end on a fixture state)
# --------------------------------------------------------------------------


def test_log_handler_prints_on_existing_log(tmp_path, capsys):
    from awf.core.wiki import append_log_entry

    append_log_entry(tmp_path, event_type="x", summary="entry one")
    args = _ns(repo_root=str(tmp_path), tail=None)
    assert run_wiki_log(args) == 0
    out = capsys.readouterr().out
    assert "entry one" in out


def test_lint_handler_exits_one_on_issue(tmp_path):
    from awf.core.wiki import WikiPage, write_page

    write_page(
        wiki_root(tmp_path) / "decisions" / "no-prov.md",
        WikiPage(frontmatter={"title": "x"}, body="body"),
    )
    args = _ns(repo_root=str(tmp_path), stale_days=30, json=False)
    assert run_wiki_lint(args) == 1


def test_regenerate_index_handler_creates_index(tmp_path, capsys):
    args = _ns(repo_root=str(tmp_path))
    assert run_wiki_regenerate_index(args) == 0
    out = capsys.readouterr().out
    assert "regenerated" in out


# --------------------------------------------------------------------------
# compile
# --------------------------------------------------------------------------


def _compile_ns(tmp_path, **overrides) -> argparse.Namespace:
    base = dict(
        repo_root=str(tmp_path),
        since=None,
        topic=None,
        dry_run=False,
        show_body=False,
        json=False,
    )
    base.update(overrides)
    return _ns(**base)


def test_compile_handler_no_events_reports_skipped(tmp_path, capsys):
    from awf.commands.wiki import run_wiki_compile

    assert run_wiki_compile(_compile_ns(tmp_path)) == 0
    out = capsys.readouterr().out
    # Header line + per-topic skip notes.
    assert "compiled 0/" in out
    assert "no_events_in_window" in out


def test_compile_handler_writes_pages_for_each_event_type(tmp_path):
    from awf.commands.wiki import run_wiki_compile
    from awf.core.operational_metrics import record_event
    from awf.core.wiki import wiki_root

    now = "2026-05-15T12:00:00+00:00"
    record_event(tmp_path, "stage1_invalidation", {
        "service": "x", "transitive_enabled": True,
        "direct_count": 1, "indirect_count": 1, "invalidating_count": 1,
        "unchanged_count": 0, "deleted_count": 0,
    }, ts=now)
    record_event(tmp_path, "scope_check", {
        "base_branch": "main", "planned_count": 3, "expanded_count": 4,
        "changed_count": 2, "violation_count": 0, "violation_paths": [],
        "planned_not_changed_count": 1,
    }, ts=now)

    assert run_wiki_compile(_compile_ns(tmp_path)) == 0
    op_dir = wiki_root(tmp_path) / "operations"
    assert (op_dir / "stage1-invalidation.md").exists()
    assert (op_dir / "scope-check.md").exists()
    # Topics with no matching events did not write a file.
    assert not (op_dir / "dispatch-performance.md").exists()


def test_compile_handler_topic_flag_restricts(tmp_path):
    from awf.commands.wiki import run_wiki_compile
    from awf.core.operational_metrics import record_event
    from awf.core.wiki import wiki_root

    now = "2026-05-15T12:00:00+00:00"
    record_event(tmp_path, "stage1_invalidation", {
        "direct_count": 1, "indirect_count": 1, "invalidating_count": 1,
        "unchanged_count": 0, "deleted_count": 0, "transitive_enabled": True,
    }, ts=now)
    record_event(tmp_path, "scope_check", {
        "base_branch": "main", "planned_count": 1, "expanded_count": 1,
        "changed_count": 1, "violation_count": 0, "violation_paths": [],
        "planned_not_changed_count": 0,
    }, ts=now)

    args = _compile_ns(tmp_path, topic="stage1-invalidation")
    assert run_wiki_compile(args) == 0
    op_dir = wiki_root(tmp_path) / "operations"
    assert (op_dir / "stage1-invalidation.md").exists()
    assert not (op_dir / "scope-check.md").exists()


def test_compile_handler_unknown_topic_returns_two(tmp_path, capsys):
    from awf.commands.wiki import run_wiki_compile

    args = _compile_ns(tmp_path, topic="not-real")
    assert run_wiki_compile(args) == 2
    err = capsys.readouterr().err
    assert "unknown topic" in err


def test_compile_handler_dry_run_writes_nothing_but_reports(tmp_path, capsys):
    from awf.commands.wiki import run_wiki_compile
    from awf.core.operational_metrics import record_event
    from awf.core.wiki import wiki_root

    now = "2026-05-15T12:00:00+00:00"
    record_event(tmp_path, "dual_strategy_engaged",
                 {"phase": "review", "promoted_from": "solo", "promoted_to": "cross"},
                 ts=now)

    args = _compile_ns(tmp_path, dry_run=True)
    assert run_wiki_compile(args) == 0
    out = capsys.readouterr().out
    assert "dry-run:" in out
    # Nothing on disk
    assert not (wiki_root(tmp_path) / "operations" / "dual-strategy-promotions.md").exists()


def test_compile_handler_json_output_is_machine_parseable(tmp_path, capsys):
    from awf.commands.wiki import run_wiki_compile
    from awf.core.operational_metrics import record_event

    now = "2026-05-15T12:00:00+00:00"
    record_event(tmp_path, "dispatch_complete", {
        "backend": "cmux", "strategy": "single", "mode": "cross",
        "worker_count": 1, "success_count": 1, "timed_out_count": 0,
        "total_seconds": 7.5,
    }, ts=now)

    args = _compile_ns(tmp_path, json=True)
    assert run_wiki_compile(args) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    written = [p for p in payload if p["written"]]
    assert any(p["topic"] == "dispatch-performance" for p in written)


def test_compile_handler_since_filter_passes_through(tmp_path):
    from awf.commands.wiki import run_wiki_compile
    from awf.core.operational_metrics import record_event
    from awf.core.wiki import read_page, wiki_root
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.now(timezone.utc)
    old_ts = (now_dt - timedelta(days=180)).isoformat()
    new_ts = (now_dt - timedelta(days=2)).isoformat()
    base = {
        "service": "x", "transitive_enabled": True,
        "direct_count": 1, "indirect_count": 1, "invalidating_count": 1,
        "unchanged_count": 0, "deleted_count": 0,
    }
    record_event(tmp_path, "stage1_invalidation", base, ts=old_ts)
    record_event(tmp_path, "stage1_invalidation", base, ts=new_ts)

    args = _compile_ns(tmp_path, since=7)  # only the 2-day-old event qualifies
    assert run_wiki_compile(args) == 0
    page = read_page(wiki_root(tmp_path) / "operations" / "stage1-invalidation.md")
    assert page.frontmatter["event_count"] == "1"
