from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "cli" / "src"))

from awf.core.skill_pressure import (  # noqa: E402
    EvidenceError,
    Verdict,
    build_evidence_matrix,
    deterministic_report_path,
    discovery_report_path,
    install_report_path,
    load_skill_matrix,
    validate_evidence_matrix,
    validate_source_bundle,
    write_evidence_summary,
)


MATRIX_RELATIVE_PATH = Path("cli/tests/fixtures/skill-validation-matrix.v1.json")


def _read_object(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} report could not be loaded") from exc
    if not isinstance(payload, Mapping):
        raise EvidenceError(f"{label} report must be an object")
    return payload


def _read_source_snapshot(
    reference: object, *, label: str
) -> Mapping[str, Any]:
    if not isinstance(reference, Mapping):
        raise EvidenceError(f"invalid {label} source reference")
    path_value = reference.get("path")
    expected_sha256 = reference.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
        raise EvidenceError(f"invalid {label} source reference")
    try:
        raw = Path(path_value).read_bytes()
    except OSError as exc:
        raise EvidenceError(f"{label} source could not be loaded") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise EvidenceError(f"{label} source hash mismatch")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{label} source could not be loaded") from exc
    if not isinstance(payload, Mapping):
        raise EvidenceError(f"{label} source must be an object")
    return payload


def _field_batch_id(report: Mapping[str, Any]) -> object:
    if report.get("persistence_status") == "COMPLETE":
        payload = report.get("payload")
    elif report.get("persistence_status") == "BLOCKED":
        payload = report.get("field_identity")
    else:
        payload = None
    return payload.get("batch_id") if isinstance(payload, Mapping) else None


def _field_paths(repo_root: Path, batch_id: str) -> list[Path]:
    root = deterministic_report_path(repo_root, batch_id).parent
    paths: list[Path] = []
    for path in sorted(root.glob(f"{batch_id}-*.json")):
        report = _read_object(path, label="field")
        if _field_batch_id(report) == batch_id:
            paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the exact current-batch 15 by 9 AWF Skill evidence summary."
    )
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--repo-root", required=True)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    matrix_path = repo_root / MATRIX_RELATIVE_PATH
    try:
        matrix = load_skill_matrix(matrix_path)
        deterministic_path = deterministic_report_path(repo_root, args.batch_id)
        install_path = install_report_path(repo_root, args.batch_id)
        discovery_path = discovery_report_path(repo_root, args.batch_id)
        field_paths = _field_paths(repo_root, args.batch_id)
        sources = validate_source_bundle(
            batch_id=args.batch_id,
            deterministic_path=deterministic_path,
            install_path=install_path,
            discovery_path=discovery_path,
            field_paths=field_paths,
        )
        _read_source_snapshot(sources["deterministic"], label="deterministic")
        _read_source_snapshot(sources["install"], label="install")
        discovery = _read_source_snapshot(sources["discovery"], label="discovery")
        field_references = sources["field"]
        if not isinstance(field_references, tuple):
            raise EvidenceError("invalid field source references")
        field = [
            _read_source_snapshot(reference, label="field")
            for reference in field_references
        ]
        discovery_records = discovery.get("records")
        if not isinstance(discovery_records, list) or not all(
            isinstance(record, Mapping) for record in discovery_records
        ):
            raise EvidenceError("discovery report records must be a list")
        cells = build_evidence_matrix(
            matrix,
            deterministic_pass=True,
            install_pass=True,
            discovery=discovery_records,
            field=field,
        )
        validate_evidence_matrix(matrix, cells)
        if any(cell.verdict in {Verdict.FAIL, Verdict.BLOCKED} for cell in cells):
            raise EvidenceError("current-batch evidence contains FAIL or BLOCKED")
        summary = write_evidence_summary(
            repo_root,
            run_id=args.batch_id,
            cells=cells,
            sources=sources,
            matrix=matrix,
        )
    except (EvidenceError, FileExistsError, OSError) as exc:
        print(f"evidence build failed: {exc}", file=sys.stderr)
        return 1

    counts = Counter(cell.verdict.value for cell in cells)
    print(f"{summary} verdict_counts={json.dumps(dict(sorted(counts.items())), sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
