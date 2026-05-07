from __future__ import annotations

import subprocess
from pathlib import Path

from awf.tools.base import ToolResult


class GitOpsToolset:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _run(self, *args: str) -> ToolResult:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                check=False,
            )
            return ToolResult(
                ok=completed.returncode == 0,
                output=completed.stdout,
                error=completed.stderr,
            )
        except Exception as exc:
            return ToolResult(ok=False, output="", error=str(exc))

    def diff(self, *args: str) -> ToolResult:
        return self._run("diff", *args)

    def log(self, *args: str) -> ToolResult:
        return self._run("log", *args)
