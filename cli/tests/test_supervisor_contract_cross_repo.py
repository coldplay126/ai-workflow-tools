from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest


SUPERVISOR_ROOT = Path(__file__).parents[1] / "src" / "awf" / "supervisor"
SCHEMA_ROOT = SUPERVISOR_ROOT / "schemas"
FIXTURE_ROOT = SUPERVISOR_ROOT / "fixtures"
CONTRACT_FILENAMES = (
    "agent-v1.json",
    "command-v1.json",
    "event-v1.json",
    "job-v1.json",
    "state-machine-v1.json",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_aws_control_plane_contract_manifest_matches_awf() -> None:
    aws_root = os.environ.get("AWF_AWS_AGENT_POC_ROOT")
    if not aws_root:
        pytest.skip("set AWF_AWS_AGENT_POC_ROOT for cross-project contract verification")

    expected_files = {
        **{
            f"{kind}-v1.json": SCHEMA_ROOT / f"{kind}-v1.json"
            for kind in ("agent", "command", "event", "job")
        },
        "state-machine-v1.json": FIXTURE_ROOT / "state-machine-v1.json",
    }
    contract_root = Path(aws_root) / "supervisor" / "contracts"
    manifest = json.loads((contract_root / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert set(manifest) == {"schema_version", "files"}
    assert set(manifest["files"]) == set(CONTRACT_FILENAMES)
    assert set(manifest["files"]) == set(expected_files)

    for filename, source in expected_files.items():
        expected_digest = _sha256(source)
        assert manifest["files"][filename] == expected_digest, filename
        assert _sha256(contract_root / filename) == expected_digest, filename
