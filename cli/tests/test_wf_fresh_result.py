"""§1.4 _find_fresh_result_file tests."""

from __future__ import annotations

import os
import time
from pathlib import Path

from awf.commands.wf import _find_fresh_result_file


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / ".workflow" / "tmp").mkdir(parents=True)
    return tmp_path


def test_no_tmp_dir_returns_none(tmp_path: Path) -> None:
    assert _find_fresh_result_file(str(tmp_path), "verify") is None


def test_no_result_files_returns_none(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert _find_fresh_result_file(str(repo), "verify") is None


def test_recent_result_file_is_returned(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo / ".workflow" / "tmp" / "result-verify-claude-code.txt"
    result.write_text("{}", encoding="utf-8")
    fresh = _find_fresh_result_file(str(repo), "verify")
    assert fresh == result


def test_stale_result_file_returns_none(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = repo / ".workflow" / "tmp" / "result-verify-claude-code.txt"
    result.write_text("{}", encoding="utf-8")
    # Make mtime older than max_age (default 1800s)
    old = time.time() - 7200
    os.utime(result, (old, old))
    assert _find_fresh_result_file(str(repo), "verify") is None


def test_newest_among_multiple_is_returned(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    older = repo / ".workflow" / "tmp" / "result-impl-claude-code.txt"
    newer = repo / ".workflow" / "tmp" / "result-impl-codex.txt"
    older.write_text("{}", encoding="utf-8")
    older_mtime = time.time() - 600
    os.utime(older, (older_mtime, older_mtime))
    newer.write_text("{}", encoding="utf-8")
    fresh = _find_fresh_result_file(str(repo), "impl")
    assert fresh == newer


def test_phase_filter_excludes_other_phases(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".workflow" / "tmp" / "result-review-claude-code.txt").write_text("{}", encoding="utf-8")
    assert _find_fresh_result_file(str(repo), "verify") is None
