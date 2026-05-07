from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolResult:
    ok: bool
    output: str
    error: str = ""
