"""§3.5 result file naming — accumulating per-round + timestamp."""

from __future__ import annotations

import json
import re
from pathlib import Path

from awf.core.workflow_prompt import save_workflow_result


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / ".workflow" / "tmp").mkdir(parents=True)
    return tmp_path


def _write_state(repo: Path, phase: str, retries: int) -> None:
    (repo / ".workflow" / "state.json").write_text(
        json.dumps({
            "id": "demo",
            "currentPhase": phase,
            "phases": {phase: {"status": "in_progress", "retries": retries}},
            "gates": {},
            "history": [],
            "totalExecutions": 0,
        }),
        encoding="utf-8",
    )


def test_filename_includes_round_and_timestamp(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write_state(repo, "verify", retries=2)
    path = save_workflow_result(str(repo), "verify", "claude-code", "hello")
    assert path.parent == repo / ".workflow" / "tmp"
    # result-verify-r{round}-{epoch_ms}-{provider}.txt
    m = re.match(r"^result-verify-r(\d+)-(\d+)-claude-code\.txt$", path.name)
    assert m is not None, f"unexpected filename: {path.name}"
    assert int(m.group(1)) == 2  # round from retries
    assert int(m.group(2)) > 1_000_000_000_000  # epoch_ms is ~ms-scale
    assert path.read_text(encoding="utf-8") == "hello"


def test_round_defaults_to_zero_when_state_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = save_workflow_result(str(repo), "review", "claude-code", "x")
    assert "-r0-" in path.name


def test_two_calls_in_same_round_produce_distinct_files(tmp_path: Path) -> None:
    import time

    repo = _make_repo(tmp_path)
    _write_state(repo, "verify", retries=1)
    first = save_workflow_result(str(repo), "verify", "claude-code", "1")
    time.sleep(0.005)  # ensure different epoch_ms even on fast machines
    second = save_workflow_result(str(repo), "verify", "claude-code", "2")
    assert first != second
    assert first.exists() and second.exists()
    assert first.read_text() == "1"
    assert second.read_text() == "2"


def test_explicit_round_index_overrides_state(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write_state(repo, "verify", retries=99)
    path = save_workflow_result(str(repo), "verify", "claude-code", "x", round_index=7)
    assert "-r7-" in path.name


def test_negative_round_clamps_to_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = save_workflow_result(str(repo), "review", "claude-code", "x", round_index=-3)
    assert "-r0-" in path.name


def test_provider_colon_is_sanitised(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = save_workflow_result(str(repo), "review", "claude:sonnet", "x")
    assert "-claude_sonnet.txt" in path.name
    assert ":" not in path.name
