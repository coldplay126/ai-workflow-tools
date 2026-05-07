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
                'fallback = ["codex"]',
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
                '[mcp."fixture_mcp"]',
                'type = "stdio"',
                'command = "python3"',
                f'args = ["-u", "{ROOT / "cli" / "tests" / "fixtures" / "fixture_mcp_server.py"}"]',
                "timeout = 5",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _run_awf(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_CLAUDE_COMMAND"] = "/bin/echo"
    env["AWF_CODEX_COMMAND"] = "/bin/echo"
    env["AWF_TEST_ANTHROPIC_KEY"] = "fixture-anthropic-key"
    env["AWF_TEST_OPENAI_KEY"] = "fixture-openai-key"
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

        human = _run_awf(temp_repo, "doctor")
        print(human.stdout, end="")
        if human.stderr:
            print(human.stderr, file=sys.stderr, end="")
        if human.returncode != 0:
            return human.returncode
        if "default_provider: fixture" not in human.stdout:
            return 1
        if "session_db:" not in human.stdout:
            return 1
        if "providers:" not in human.stdout:
            return 1

        raw = _run_awf(temp_repo, "doctor", "--json")
        print(raw.stdout, end="")
        if raw.stderr:
            print(raw.stderr, file=sys.stderr, end="")
        if raw.returncode != 0:
            return raw.returncode
        payload = json.loads(raw.stdout)
        print(f"doctor_default_provider={payload.get('default_provider')}")
        print(f"doctor_mcp_server_count={payload.get('mcp', {}).get('server_count')}")
        providers = {item["provider"]: item for item in payload.get("providers", [])}
        if payload.get("default_provider") != "fixture":
            return 1
        if payload.get("mcp", {}).get("server_count") != 1:
            return 1
        if payload.get("paths", {}).get("session_db") != str(session_db):
            return 1
        if providers.get("claude-code", {}).get("installed", {}).get("status") != "ok":
            return 1
        if providers.get("codex", {}).get("installed", {}).get("status") != "ok":
            return 1
        if providers.get("claude-sdk", {}).get("configured", {}).get("status") != "ok":
            return 1
        if providers.get("openai", {}).get("configured", {}).get("status") != "ok":
            return 1
        if providers.get("fixture", {}).get("installed", {}).get("status") != "ok":
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
