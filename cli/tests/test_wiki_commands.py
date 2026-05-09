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
