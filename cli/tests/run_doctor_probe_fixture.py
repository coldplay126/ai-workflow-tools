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
                'default = "fixture"',
                "",
                "[provider.fixture]",
                'result_file = "cli/tests/fixtures/review-result.json"',
                "",
                "[provider.claude-sdk]",
                'api_key_env = "AWF_TEST_ANTHROPIC_KEY"',
                "",
                "[provider.openai]",
                'api_key_env = "AWF_TEST_OPENAI_KEY"',
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
    env["AWF_TEST_ANTHROPIC_KEY"] = "fixture-anthropic-key"
    env["AWF_TEST_OPENAI_KEY"] = "fixture-openai-key"
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

        probed = _run_awf(temp_repo, "doctor", "--json", "--probe")
        print(probed.stdout, end="")
        if probed.stderr:
            print(probed.stderr, file=sys.stderr, end="")
        if probed.returncode != 0:
            return probed.returncode
        payload = json.loads(probed.stdout)
        providers = {item["provider"]: item for item in payload.get("providers", [])}
        print(f"doctor_probe_enabled={payload.get('probe_enabled')}")
        if payload.get("probe_enabled") is not True:
            return 1
        if providers.get("claude-code", {}).get("probe", {}).get("status") != "ok":
            return 1
        if providers.get("codex", {}).get("probe", {}).get("status") != "ok":
            return 1
        if providers.get("claude-sdk", {}).get("probe", {}).get("status") != "skip":
            return 1
        if providers.get("openai", {}).get("probe", {}).get("status") != "skip":
            return 1
        if providers.get("fixture", {}).get("probe", {}).get("status") != "ok":
            return 1

        hinted = _run_awf(
            temp_repo,
            "chat",
            "--provider",
            "claude-code",
            "--message",
            "hello",
            extra_env={"AWF_CLAUDE_COMMAND": "/definitely-missing-command"},
        )
        print(hinted.stdout, end="")
        if hinted.stderr:
            print(hinted.stderr, file=sys.stderr, end="")
        if hinted.returncode == 0:
            return 1
        if "hint: run `awf doctor --repo-root .`" not in hinted.stderr:
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
