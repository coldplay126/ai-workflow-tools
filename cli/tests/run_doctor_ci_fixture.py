from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _prepare_temp_repo(temp_repo: Path, session_db: Path) -> None:
    (temp_repo / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "docs" / "architecture" / "awf-cli-architecture.md", temp_repo / "docs" / "awf-cli-architecture.md")
    (temp_repo / ".awf.toml").write_text(
        "\n".join(
            [
                "[provider]",
                'default = "claude-code"',
                "",
                "[provider.fixture]",
                'result_file = "cli/tests/fixtures/review-result.json"',
                "",
                "[paths]",
                f'session_db = "{session_db}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _run_awf(repo_root: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_CLAUDE_COMMAND"] = "/bin/echo"
    env["AWF_CODEX_COMMAND"] = "/bin/echo"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "awf", *args, "--repo-root", str(repo_root)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        temp_repo = (Path(tmp_dir_str) / "repo").resolve()
        session_db = temp_repo / ".tmp" / "awf.db"
        temp_repo.mkdir(parents=True, exist_ok=True)
        _prepare_temp_repo(temp_repo, session_db)

        healthy = _run_awf(temp_repo, "doctor", "--json", "--ci")
        print(healthy.stdout, end="")
        if healthy.stderr:
            print(healthy.stderr, file=sys.stderr, end="")
        if healthy.returncode != 0:
            return healthy.returncode
        payload = json.loads(healthy.stdout)
        if payload.get("ci", {}).get("ok") is not True:
            return 1
        print(f"doctor_ci_ok={payload['ci']['ok']}")

        healthy_probe = _run_awf(temp_repo, "doctor", "--json", "--ci", "--probe")
        print(healthy_probe.stdout, end="")
        if healthy_probe.stderr:
            print(healthy_probe.stderr, file=sys.stderr, end="")
        if healthy_probe.returncode != 0:
            return healthy_probe.returncode
        payload_probe = json.loads(healthy_probe.stdout)
        if payload_probe.get("ci", {}).get("ok") is not True:
            return 1
        print(f"doctor_ci_probe_ok={payload_probe['ci']['ok']}")

        broken = _run_awf(
            temp_repo,
            "doctor",
            "--json",
            "--ci",
            extra_env={"AWF_CLAUDE_COMMAND": "/definitely-missing-command"},
        )
        print(broken.stdout, end="")
        if broken.stderr:
            print(broken.stderr, file=sys.stderr, end="")
        if broken.returncode == 0:
            return 1
        broken_payload = json.loads(broken.stdout)
        if broken_payload.get("ci", {}).get("ok") is not False:
            return 1
        if "failed" not in str(broken_payload.get("ci", {}).get("reason", "")):
            return 1
        print(f"doctor_ci_fail_reason={broken_payload['ci']['reason']}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
