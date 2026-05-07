from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from awf.tools.base import ToolResult


class FileOpsToolset:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if self.root != path and self.root not in path.parents:
            raise PermissionError(f"path escapes tool root: {relative_path}")
        return path

    def read(self, relative_path: str) -> ToolResult:
        try:
            path = self._resolve(relative_path)
            return ToolResult(ok=True, output=path.read_text(encoding="utf-8"))
        except Exception as exc:
            return ToolResult(ok=False, output="", error=str(exc))

    def write(self, relative_path: str, content: str) -> ToolResult:
        try:
            path = self._resolve(relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(ok=True, output=str(path))
        except Exception as exc:
            return ToolResult(ok=False, output="", error=str(exc))

    def glob(self, pattern: str) -> ToolResult:
        try:
            matches = [str(path.relative_to(self.root)) for path in sorted(self.root.glob(pattern))]
            return ToolResult(ok=True, output="\n".join(matches))
        except Exception as exc:
            return ToolResult(ok=False, output="", error=str(exc))

    def grep(self, pattern: str, paths: Optional[Iterable[str]] = None) -> ToolResult:
        try:
            targets = list(paths or [])
            if not targets:
                file_paths = [path for path in self.root.rglob("*") if path.is_file()]
            else:
                file_paths = [self._resolve(item) for item in targets]

            matches: list[str] = []
            for path in file_paths:
                try:
                    content = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                for lineno, line in enumerate(content.splitlines(), start=1):
                    if pattern in line:
                        matches.append(f"{path.relative_to(self.root)}:{lineno}:{line}")
            return ToolResult(ok=True, output="\n".join(matches))
        except Exception as exc:
            return ToolResult(ok=False, output="", error=str(exc))
