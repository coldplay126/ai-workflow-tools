from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _prepare_temp_repo(temp_repo: Path) -> None:
    (temp_repo / "docs").mkdir(parents=True, exist_ok=True)
    temp_docs_root = temp_repo.parent / "analysis-docs"
    temp_docs_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "docs" / "architecture" / "awf-cli-architecture.md", temp_repo / "docs" / "awf-cli-architecture.md")
    skill_dir = temp_repo / ".awf" / "skills" / "fixture-docs"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: fixture-docs",
                "description: Fixture discovery skill",
                "---",
                "",
                "# Fixture Skill",
                "",
                "Used for discovery fixture testing.",
                "",
            ]
        ),
        encoding="utf-8",
    )
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
                "[paths]",
                f'analysis_docs = "{temp_docs_root}"',
                f'awf_github = "{temp_repo.parent}"',
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
        temp_repo.mkdir(parents=True, exist_ok=True)
        _prepare_temp_repo(temp_repo)

        shown = _run_awf(temp_repo, "config", "show", "--json")
        print(shown.stdout, end="")
        if shown.stderr:
            print(shown.stderr, file=sys.stderr, end="")
        if shown.returncode != 0:
            return shown.returncode
        config_payload = json.loads(shown.stdout)
        print(f"config_provider_default={config_payload.get('provider_default')}")
        print(f"config_mcp_count={len(config_payload.get('mcp', {}))}")
        if config_payload.get("provider_default") != "fixture":
            return 1
        if len(config_payload.get("mcp", {})) != 1:
            return 1
        if config_payload.get("paths", {}).get("repo_root") != str(temp_repo.resolve()):
            return 1

        skills = _run_awf(temp_repo, "skills", "list", "--json")
        print(skills.stdout, end="")
        if skills.stderr:
            print(skills.stderr, file=sys.stderr, end="")
        if skills.returncode != 0:
            return skills.returncode
        skills_payload = json.loads(skills.stdout)
        skill_names = [item.get("name") for item in skills_payload.get("skills", [])]
        print(f"skills_count={len(skill_names)}")
        print(f"skills_names={','.join(skill_names)}")
        if "fixture-docs" not in skill_names:
            return 1

        mcp = _run_awf(temp_repo, "mcp", "list", "--json")
        print(mcp.stdout, end="")
        if mcp.stderr:
            print(mcp.stderr, file=sys.stderr, end="")
        if mcp.returncode != 0:
            return mcp.returncode
        mcp_payload = json.loads(mcp.stdout)
        server_names = [item.get("name") for item in mcp_payload.get("servers", [])]
        print(f"discovery_mcp_servers={','.join(server_names)}")
        if server_names != ["fixture_mcp"]:
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
