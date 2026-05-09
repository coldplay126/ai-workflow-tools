"""Tests for awf.core.wiki — frontmatter, log, index, lint."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.wiki import (
    PROFILE_CONSUMER,
    PROFILE_SELF_IMPROVEMENT,
    WikiPage,
    append_log_entry,
    decision_path,
    decision_template,
    index_path,
    lint,
    log_path,
    read_page,
    read_profile,
    regenerate_index,
    slugify,
    starter_directories,
    wiki_root,
    write_page,
    write_profile,
)


# --------------------------------------------------------------------------
# Frontmatter round-trip
# --------------------------------------------------------------------------


def test_write_then_read_preserves_frontmatter_and_body(tmp_path):
    page = WikiPage(
        frontmatter={
            "title": "Transitive Invalidation Trends",
            "source_runs": ["run-2026-05-08-abc"],
            "source_commits": ["95cdaed", "541e3b5"],
            "confidence": "high",
            "last_compiled_at": "2026-05-09T00:00:00+00:00",
        },
        body="# Body\n\nFindings…\n",
    )
    target = wiki_root(tmp_path) / "operations" / "transitive-invalidation.md"
    write_page(target, page)
    loaded = read_page(target)
    assert loaded.frontmatter["title"] == "Transitive Invalidation Trends"
    assert loaded.frontmatter["source_commits"] == ["95cdaed", "541e3b5"]
    assert loaded.frontmatter["confidence"] == "high"
    assert "Findings…" in loaded.body


def test_read_page_handles_missing_frontmatter(tmp_path):
    target = wiki_root(tmp_path) / "no-front.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("just body, no frontmatter\n", encoding="utf-8")
    page = read_page(target)
    assert page.frontmatter == {}
    assert "just body" in page.body


# --------------------------------------------------------------------------
# log.md append-only invariant
# --------------------------------------------------------------------------


def test_append_log_entry_creates_header_and_appends(tmp_path):
    target = log_path(tmp_path)
    assert not target.exists()
    append_log_entry(tmp_path, event_type="stage1_invalidation", summary="3 direct, 1 indirect")
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Operations log\n")
    assert "## [" in text
    assert "stage1_invalidation | 3 direct, 1 indirect" in text


def test_append_log_entry_preserves_prior_entries(tmp_path):
    append_log_entry(tmp_path, event_type="a", summary="first")
    append_log_entry(tmp_path, event_type="b", summary="second")
    append_log_entry(tmp_path, event_type="c", summary="third")
    text = log_path(tmp_path).read_text(encoding="utf-8")
    # All three event lines must be present in the order they were appended.
    a_idx = text.index("a | first")
    b_idx = text.index("b | second")
    c_idx = text.index("c | third")
    assert a_idx < b_idx < c_idx


# --------------------------------------------------------------------------
# index.md regeneration
# --------------------------------------------------------------------------


def test_regenerate_index_groups_by_subdir(tmp_path):
    write_page(
        wiki_root(tmp_path) / "operations" / "trans-inv.md",
        WikiPage(
            frontmatter={"title": "Trans Inv", "last_compiled_at": "2026-05-09T00:00:00+00:00"},
            body="body\n",
        ),
    )
    write_page(
        wiki_root(tmp_path) / "concepts" / "dispatch.md",
        WikiPage(frontmatter={"title": "Dispatch"}, body="body\n"),
    )
    target = regenerate_index(tmp_path)
    text = target.read_text(encoding="utf-8")
    assert "## concepts" in text
    assert "## operations" in text
    assert "[Trans Inv](wiki/operations/trans-inv.md)" in text
    assert "[Dispatch](wiki/concepts/dispatch.md)" in text
    assert "_last compiled 2026-05-09T00:00:00+00:00_" in text


def test_regenerate_index_with_empty_wiki(tmp_path):
    target = regenerate_index(tmp_path)
    text = target.read_text(encoding="utf-8")
    assert "_No pages yet._" in text


# --------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------


def _make_page(tmp_path, relpath: str, fm: dict, body: str = "body\n") -> Path:
    target = wiki_root(tmp_path) / relpath
    write_page(target, WikiPage(frontmatter=fm, body=body))
    return target


def test_lint_clean_when_provenance_and_index_present(tmp_path):
    _make_page(
        tmp_path,
        "operations/recent.md",
        {
            "title": "Recent",
            "source_commits": ["abc1234"],
            "last_compiled_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    regenerate_index(tmp_path)
    issues = lint(tmp_path)
    assert issues == []


def test_lint_flags_missing_provenance(tmp_path):
    _make_page(tmp_path, "operations/no-prov.md", {"title": "x"})
    regenerate_index(tmp_path)
    issues = lint(tmp_path)
    codes = {i.code for i in issues}
    assert "missing_provenance" in codes


def test_lint_flags_stale(tmp_path):
    old_stamp = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    _make_page(
        tmp_path,
        "operations/old.md",
        {
            "title": "Old",
            "source_commits": ["abc1234"],
            "last_compiled_at": old_stamp,
        },
    )
    regenerate_index(tmp_path)
    issues = lint(tmp_path, stale_days=30)
    codes = {i.code for i in issues}
    assert "stale" in codes


def test_lint_flags_orphan_when_index_missing_link(tmp_path):
    _make_page(
        tmp_path,
        "operations/orphan.md",
        {
            "title": "Orphan",
            "source_commits": ["abc1234"],
            "last_compiled_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    # Deliberately do NOT regenerate the index.
    issues = lint(tmp_path)
    codes = {i.code for i in issues}
    assert "orphan" in codes


def test_lint_returns_empty_when_no_wiki_dir(tmp_path):
    assert lint(tmp_path) == []


# --------------------------------------------------------------------------
# Profile + slugify + decision template
# --------------------------------------------------------------------------


def test_read_profile_defaults_to_consumer_when_no_marker(tmp_path):
    assert read_profile(tmp_path) == PROFILE_CONSUMER


def test_write_then_read_profile_round_trips(tmp_path):
    write_profile(tmp_path, PROFILE_SELF_IMPROVEMENT)
    assert read_profile(tmp_path) == PROFILE_SELF_IMPROVEMENT


def test_write_profile_rejects_unknown_value(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="unknown wiki profile"):
        write_profile(tmp_path, "sideways")


def test_starter_directories_differs_by_profile():
    self_dirs = starter_directories(PROFILE_SELF_IMPROVEMENT)
    consumer_dirs = starter_directories(PROFILE_CONSUMER)
    # decisions and operations are common; concepts vs services is the split.
    assert "decisions" in self_dirs and "decisions" in consumer_dirs
    assert "concepts" in self_dirs and "concepts" not in consumer_dirs
    assert "services" in consumer_dirs and "services" not in self_dirs


def test_slugify_handles_korean_and_punctuation():
    # Korean is stripped (no transliteration); ASCII portion survives.
    assert slugify("dispatch lifecycle — 한국어 mixed!") == "dispatch-lifecycle-mixed"
    assert slugify("Option A vs B") == "option-a-vs-b"
    assert slugify("---!!!") == "decision"


def test_decision_template_self_improvement_has_options_section(tmp_path):
    page = decision_template(
        profile=PROFILE_SELF_IMPROVEMENT,
        title="Use stdlib for cmux bridge",
        context_prs=["#26"],
    )
    assert page.frontmatter["title"] == "Use stdlib for cmux bridge"
    assert page.frontmatter["status"] == "proposed"
    assert page.frontmatter["context_prs"] == ["#26"]
    assert "Options considered" in page.body


def test_decision_template_consumer_focuses_on_configuration(tmp_path):
    page = decision_template(
        profile=PROFILE_CONSUMER,
        title="Tune surface_preference for service-x",
    )
    assert "Configuration / policy chosen" in page.body
    assert "Rejected alternatives" in page.body


def test_decision_template_appends_pr_body_when_provided():
    page = decision_template(
        profile=PROFILE_CONSUMER,
        title="x",
        pr_body="Title: foo\nbody text here",
    )
    assert "Source PR excerpt" in page.body
    assert "body text here" in page.body


def test_decision_path_uses_date_and_slug(tmp_path):
    path = decision_path(tmp_path, title="Use stdlib only", date="2026-05-09")
    assert path.name == "2026-05-09-use-stdlib-only.md"
    assert path.parent == wiki_root(tmp_path) / "decisions"
