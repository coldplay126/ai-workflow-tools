from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SERVER = ROOT / "cli" / "tests" / "fixtures" / "fixture_mcp_server.py"
SERVER_NAME = "fixture_mcp"


def _run_awf(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    return subprocess.run(
        [sys.executable, "-m", "awf", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def main() -> int:
    config_path = ROOT / ".awf.toml"
    backup = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    config_path.write_text(
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

    try:
        checked = _run_awf("mcp", "check", SERVER_NAME, "--repo-root", str(ROOT))
        print(checked.stdout, end="")
        if checked.stderr:
            print(checked.stderr, file=sys.stderr, end="")
        if checked.returncode != 0:
            return checked.returncode

        invoked = _run_awf(
            "mcp",
            "invoke",
            SERVER_NAME,
            "echo",
            "--input",
            '{"text":"hello"}',
            "--json",
            "--repo-root",
            str(ROOT),
        )
        print(invoked.stdout, end="")
        if invoked.stderr:
            print(invoked.stderr, file=sys.stderr, end="")
        if invoked.returncode != 0:
            return invoked.returncode

        payload = json.loads(invoked.stdout)
        print(f"invoke_server={payload.get('server')}")
        print(f"invoke_tool={payload.get('tool')}")
        content = payload.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""
        print(f"invoke_text={text}")

        read_back = _run_awf(
            "mcp",
            "read",
            SERVER_NAME,
            "fixture://resource",
            "--json",
            "--repo-root",
            str(ROOT),
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
        return 0
    finally:
        if backup is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
