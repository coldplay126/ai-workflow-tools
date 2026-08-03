from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "cli" / "src"))

from awf.core.skill_pressure import (  # noqa: E402
    DETERMINISTIC_PYTEST_ARGV,
    DETERMINISTIC_SOURCE_FILES,
    EvidenceError,
    deterministic_report_path,
    sha256_file,
    write_deterministic_report,
)




def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_hashes(repo_root: Path) -> dict[str, str]:
    return {
        relative: sha256_file(repo_root / relative)
        for relative in DETERMINISTIC_SOURCE_FILES
    }


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the exact deterministic AWF Skill audit and persist its source-hashed report."
    )
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--timeout-sec", required=True, type=int)
    args = parser.parse_args(argv)
    if args.timeout_sec <= 0:
        parser.error("--timeout-sec must be positive")

    repo_root = Path(args.repo_root).absolute()
    try:
        report_path = deterministic_report_path(repo_root, args.batch_id)
    except EvidenceError as exc:
        parser.error(str(exc))
    if report_path.exists():
        print(f"deterministic report already exists: {report_path}", file=sys.stderr)
        return 1

    started_at = _utc_now()
    started = time.monotonic()
    status = 127
    stdout = ""
    stderr = ""
    written: Path | None = None
    write_error: Exception | None = None
    sources_before_execution: dict[str, str] | None = None
    try:
        sources_before_execution = _source_hashes(repo_root)
        completed = subprocess.run(
            list(DETERMINISTIC_PYTEST_ARGV),
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout_sec,
        )
        status = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        status = 124
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr) or f"deterministic_timeout after {args.timeout_sec}s"
    except OSError as exc:
        status = 127
        stderr = f"deterministic_launch_error:{type(exc).__name__}"
    finally:
        finished_at = _utc_now()
        try:
            sources_after_execution = _source_hashes(repo_root)
            sources_before_publication = _source_hashes(repo_root)
            if (
                sources_before_execution != sources_after_execution
                or sources_before_execution != sources_before_publication
            ):
                if status == 0:
                    status = 1
                stderr = (
                    f"{stderr}\ndeterministic_source_changed"
                    if stderr
                    else "deterministic_source_changed"
                )
            def write_report(sources: dict[str, str]) -> Path:
                def verify_sources_before_publish() -> None:
                    if _source_hashes(repo_root) != sources:
                        raise EvidenceError("deterministic source hash mismatch before publication")

                return write_deterministic_report(
                    repo_root,
                    batch_id=args.batch_id,
                    argv=DETERMINISTIC_PYTEST_ARGV,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_sec=time.monotonic() - started,
                    exit_status=status,
                    stdout=stdout,
                    stderr=stderr,
                    matrix_sha256=sources[
                        "cli/tests/fixtures/skill-validation-matrix.v1.json"
                    ],
                    sources=sources,
                    before_publish=verify_sources_before_publish,
                )

            try:
                written = write_report(sources_before_publication)
            except EvidenceError:
                sources_before_retry = _source_hashes(repo_root)
                if sources_before_retry == sources_before_publication:
                    raise
                if status == 0:
                    status = 1
                stderr = (
                    f"{stderr}\ndeterministic_source_changed"
                    if stderr
                    else "deterministic_source_changed"
                )
                written = write_report(sources_before_retry)
        except (EvidenceError, FileExistsError, OSError) as exc:
            write_error = exc
    if write_error is not None or written is None:
        print(f"deterministic report failed: {write_error}", file=sys.stderr)
        return 1
    print(f"{written} status={status}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
