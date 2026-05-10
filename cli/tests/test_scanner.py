from __future__ import annotations

import argparse
import json
from pathlib import Path

from awf.commands.scan import run_scan
from awf.core.scanner import detect_language, scan_repo
from awf.core.state import _detect_manifest


def test_scan_detects_requirements_txt_python_script_units(tmp_path: Path) -> None:
    repo = tmp_path / "trip"
    repo.mkdir()
    (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")
    for directory in ("collectors", "analyzers", "importers", "exporters", "monitors", "matchers"):
        unit = repo / directory
        unit.mkdir()
        (unit / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_module.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    language, framework = detect_language(repo)
    result = scan_repo(repo, use_ai=False)

    assert (language, framework) == ("python", "python")
    assert result.language == "python"
    assert result.framework == "python"
    assert result.unit_pattern == "{unit}"
    assert [unit.name for unit in result.units] == [
        "analyzers",
        "collectors",
        "exporters",
        "importers",
        "matchers",
        "monitors",
    ]
    assert "tests" in result.excluded

    manifest = _detect_manifest(repo)
    assert manifest["language"] == "python"
    assert manifest["test_command"] == "pytest"


def test_scan_all_includes_requirements_txt_projects(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path / "script_repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")
    collectors = repo / "collectors"
    collectors.mkdir()
    (collectors / "job.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    rc = run_scan(argparse.Namespace(
        all=True,
        github_root=str(tmp_path),
        docs_root=None,
        merge=False,
        dry_run=False,
        no_ai=True,
        repo_path=None,
    ))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["service"] == "script_repo"
    assert payload["units"][0]["name"] == "collectors"
    assert payload["units"][0]["directory"] == "collectors"
