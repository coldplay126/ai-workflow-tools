from __future__ import annotations

from pathlib import Path

from awf.core.config import AwfConfig
from awf.core.readiness import collect_doctor_report, collect_omp_readiness


def _write_probe_omp(path: Path) -> Path:
    path.write_text(
        '''#!/usr/bin/env python3
import json
import sys
if "--version" in sys.argv:
    print("omp/99.0.0")
    raise SystemExit(0)
message = {
    "role": "assistant",
    "content": [{"type": "text", "text": "AWF_OMP_OK"}],
    "provider": "fixture-provider",
    "model": "fixture-model",
    "usage": {"input": 1, "output": 1, "totalTokens": 2},
}
print(json.dumps({"type": "session", "id": "probe-session"}))
print(json.dumps({"type": "agent_end", "messages": [message]}))
''',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_collect_omp_readiness_reports_install_without_claiming_auth(
    tmp_path: Path,
    monkeypatch,
):
    fake = _write_probe_omp(tmp_path / "omp")
    monkeypatch.setenv("AWF_OMP_COMMAND", str(fake))
    readiness = collect_omp_readiness(probe=False)
    assert readiness["status"] == "ready"
    assert readiness["version"]["version"] == "omp/99.0.0"
    assert readiness["probe"]["status"] == "skip"
    assert readiness["auth"]["status"] == "skip"


def test_collect_omp_readiness_probe_verifies_model_and_auth(
    tmp_path: Path,
    monkeypatch,
):
    fake = _write_probe_omp(tmp_path / "omp")
    monkeypatch.setenv("AWF_OMP_COMMAND", str(fake))
    readiness = collect_omp_readiness(probe=True)
    assert readiness["status"] == "ready"
    assert readiness["probe"]["status"] == "ok"
    assert readiness["probe"]["provider"] == "fixture-provider"
    assert readiness["probe"]["model"] == "fixture-model"
    assert readiness["auth"]["status"] == "ok"


def test_doctor_marks_explicit_omp_dispatch_ready(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    workflow = repo / ".workflow"
    workflow.mkdir(parents=True)
    (workflow / "provider-config.json").write_text(
        '{"dispatch":{"surface_preference":"omp"}}',
        encoding="utf-8",
    )
    fake = _write_probe_omp(tmp_path / "omp")
    monkeypatch.setenv("AWF_OMP_COMMAND", str(fake))
    report = collect_doctor_report(AwfConfig.defaults(), str(repo), probe=False)
    assert report["dispatch"]["omp_backend_ready"] is True
    assert "omp" in report["dispatch"]["available_surfaces"]
    assert report["dispatch"]["surface_preference_ready"]["status"] == "ok"
    omp_runner = next(item for item in report["runners"] if item["runner"] == "omp")
    assert omp_runner["backend"]["status"] == "ok"
