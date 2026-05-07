from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SERVER = ROOT / "cli" / "tests" / "fixtures" / "fixture_mcp_server.py"
SERVER_NAME = "fixture_mcp"


def _prepare_temp_repo(temp_repo: Path) -> None:
    (temp_repo / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "docs" / "architecture" / "awf-cli-architecture.md", temp_repo / "docs" / "awf-cli-architecture.md")
    (temp_repo / ".awf.toml").write_text(
        "\n".join(
            [
                f'[mcp."{SERVER_NAME}"]',
                'type = "stdio"',
                'command = "python3"',
                f'args = ["-u", "{FIXTURE_SERVER}"]',
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
        temp_repo = Path(tmp_dir_str) / "repo"
        temp_repo.mkdir(parents=True, exist_ok=True)
        _prepare_temp_repo(temp_repo)

        listed = _run_awf(temp_repo, "mcp", "list", "--json")
        print(listed.stdout, end="")
        if listed.stderr:
            print(listed.stderr, file=sys.stderr, end="")
        if listed.returncode != 0:
            return listed.returncode
        listed_payload = json.loads(listed.stdout)
        servers = listed_payload.get("servers", [])
        print(f"mcp_list_count={len(servers)}")
        if len(servers) != 1 or servers[0].get("name") != SERVER_NAME:
            return 1

        checked = _run_awf(temp_repo, "mcp", "check", SERVER_NAME)
        print(checked.stdout, end="")
        if checked.stderr:
            print(checked.stderr, file=sys.stderr, end="")
        if checked.returncode != 0:
            return checked.returncode
        if "status: ok" not in checked.stdout:
            return 1

        invoked = _run_awf(
            temp_repo,
            "mcp",
            "invoke",
            SERVER_NAME,
            "echo",
            "--input",
            '{"text":"hello"}',
            "--json",
        )
        print(invoked.stdout, end="")
        if invoked.stderr:
            print(invoked.stderr, file=sys.stderr, end="")
        if invoked.returncode != 0:
            return invoked.returncode
        invoked_payload = json.loads(invoked.stdout)
        print(f"invoke_server={invoked_payload.get('server')}")
        print(f"invoke_tool={invoked_payload.get('tool')}")
        invoke_content = invoked_payload.get("result", {}).get("content", [])
        invoke_text = invoke_content[0].get("text", "") if invoke_content else ""
        print(f"invoke_text={invoke_text}")
        if invoke_text != "echo:hello":
            return 1

        read_back = _run_awf(
            temp_repo,
            "mcp",
            "read",
            SERVER_NAME,
            "fixture://resource",
            "--json",
        )
        print(read_back.stdout, end="")
        if read_back.stderr:
            print(read_back.stderr, file=sys.stderr, end="")
        if read_back.returncode != 0:
            return read_back.returncode
        read_payload = json.loads(read_back.stdout)
        print(f"read_server={read_payload.get('server')}")
        print(f"read_uri={read_payload.get('uri')}")
        contents = read_payload.get("result", {}).get("contents", [])
        read_text = contents[0].get("text", "") if contents else ""
        print(f"read_text={read_text}")
        if read_text != "resource:fixture://resource":
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
