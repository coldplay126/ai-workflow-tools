"""`awf dashboard` MVP — rich Live 2-panel TUI.

Layout (split_column):
  ┌─ Workflow ────────────────────┐
  │  summarize_workflow_state    │
  ├─ Cmux Broker Health ──────────┤
  │  status / pid / events / sql │
  └───────────────────────────────┘

Key bindings:
- q / Q : quit (return 0)
- r / R : force refresh (elapsed time 리셋 — M1 해소)
- Ctrl+C: KeyboardInterrupt → quit (return 0)

rich 미설치 시 stderr 안내 + return 2 (fallback 없음 — D1 watch_loop 와 다름).
"""

from __future__ import annotations

import select
import sys
import termios
import time
import tty
from typing import Optional, Protocol, runtime_checkable

from awf.core.cmux_health import probe_cmux_broker_health
from awf.core.state import load_workflow_state
from awf.core.watch_loop import clamp_interval
from awf.core.workflow_status import summarize_workflow_state


@runtime_checkable
class KeyReader(Protocol):
    """Non-blocking single-key input source. POSIX 외 환경 또는 테스트용 fake 주입 지원."""

    def read_nonblocking(self, timeout: float) -> Optional[str]:
        """Return a single character if available within ``timeout`` seconds, else None."""

    def __enter__(self) -> "KeyReader": ...

    def __exit__(self, *exc_info: object) -> None: ...


class StdinKeyReader:
    """POSIX TTY 입력 — termios cbreak 모드 + select non-blocking poll.

    Windows 미지원 (assumption 명시 — Out of Scope).
    """

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._old_settings: Optional[list] = None

    def __enter__(self) -> "StdinKeyReader":
        if not sys.stdin.isatty():
            # non-tty (CI 등) 환경에서는 cbreak 적용 시 OSError. fake reader 권장.
            return self
        try:
            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except (termios.error, OSError):
            self._fd = None
            self._old_settings = None
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None and self._old_settings is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except (termios.error, OSError):
                pass
            self._fd = None
            self._old_settings = None

    def read_nonblocking(self, timeout: float) -> Optional[str]:
        if self._fd is None:
            time.sleep(timeout)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        try:
            return sys.stdin.read(1)
        except (OSError, ValueError):
            return None


_RICH_INSTALL_HINT = (
    "error: awf dashboard requires the rich library. Install with:\n"
    "  pip install 'awf-cli[tui]'\n"
    "  or: uv tool install --with 'rich>=13.0.0' awf-cli"
)


def _try_import_rich():
    try:
        from rich.layout import Layout  # type: ignore
        from rich.live import Live  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.text import Text  # type: ignore

        return Layout, Live, Panel, Text
    except ImportError:
        return None


_BROKER_STATUS_STYLE = {
    "alive": "green",
    "fresh": "green",
    "ok": "green",
    "stale": "yellow",
    "absent": "red",
    "missing": "red",
    "error": "red",
}


def _broker_section(health: dict, key: str) -> dict:
    value = health.get(key)
    return value if isinstance(value, dict) else {}


def render_workflow_panel(repo_root, *, Panel, Text):
    """FR-005 — Workflow state summary panel."""
    try:
        state = load_workflow_state(repo_root)
    except Exception as exc:
        body = Text(f"no workflow state: {exc}")
    else:
        body = Text(summarize_workflow_state(state, repo_root=repo_root))
    return Panel(body, title="Workflow", border_style="cyan")


def render_broker_panel(repo_root, *, Panel, Text):
    """FR-006 — Cmux broker health panel (4 line + status color)."""
    health = probe_cmux_broker_health(repo_root) or {}
    daemon = _broker_section(health, "broker_daemon")
    events = _broker_section(health, "events_log")
    sqlite = _broker_section(health, "sqlite_integrity")

    status = str(daemon.get("status", "-"))
    text = Text()
    text.append(f"Status: {status}\n", style=_BROKER_STATUS_STYLE.get(status, "white"))
    text.append(f"PID: {daemon.get('pid', '-')}\n")
    events_status = str(events.get("status", "-"))
    text.append("Events log: ")
    text.append(f"{events_status}\n", style=_BROKER_STATUS_STYLE.get(events_status, "white"))
    sqlite_status = str(sqlite.get("status", "-"))
    text.append("Sqlite: ")
    text.append(f"{sqlite_status}", style=_BROKER_STATUS_STYLE.get(sqlite_status, "white"))
    return Panel(text, title="Cmux Broker Health", border_style="cyan")


def run_dashboard(
    repo_root: Optional[str],
    interval: int,
    *,
    key_reader: Optional[KeyReader] = None,
    max_iters: Optional[int] = None,
) -> int:
    """Run the dashboard event loop. Returns process exit code.

    Parameters
    ----------
    repo_root:
        Repository root (passed through to workflow_state + cmux_health).
    interval:
        Refresh interval seconds. Clamped 1~60 by ``clamp_interval``.
    key_reader:
        Optional KeyReader (POSIX stdin by default). Tests inject a FakeKeyReader.
    max_iters:
        Test-only stop after N event-loop iterations. None = loop forever.
    """
    imports = _try_import_rich()
    if imports is None:
        print(_RICH_INSTALL_HINT, file=sys.stderr)
        return 2
    Layout, Live, Panel, Text = imports

    interval = clamp_interval(interval)
    reader = key_reader if key_reader is not None else StdinKeyReader()

    layout = Layout()
    layout.split_column(Layout(name="workflow"), Layout(name="broker"))

    def refresh_panels() -> None:
        layout["workflow"].update(render_workflow_panel(repo_root, Panel=Panel, Text=Text))
        layout["broker"].update(render_broker_panel(repo_root, Panel=Panel, Text=Text))

    refresh_panels()

    tick = 0.2  # event loop granularity (sec)

    try:
        with reader, Live(layout, refresh_per_second=4, screen=False, transient=False):
            elapsed = 0.0
            iterations = 0
            while True:
                key = reader.read_nonblocking(tick)
                if key:
                    lower = key.lower()
                    if lower == "q":
                        return 0
                    if lower == "r":
                        refresh_panels()
                        elapsed = 0.0  # M1 — r 키 시 elapsed reset, 새 interval 사이클
                        iterations += 1
                        if max_iters is not None and iterations >= max_iters:
                            return 0
                        continue
                else:
                    elapsed += tick
                if elapsed >= interval:
                    refresh_panels()
                    elapsed = 0.0
                    iterations += 1
                    if max_iters is not None and iterations >= max_iters:
                        return 0
    except KeyboardInterrupt:
        return 0
