"""Watch-loop primitive for `awf wf status --watch`.

Provides ``run_watch(render_fn, interval)`` that re-renders a text payload at a
fixed interval. Uses Rich Live for flicker-free in-place refresh when ``rich``
is installed, and falls back to an ANSI clear-and-print loop otherwise.

KeyboardInterrupt (Ctrl+C) is caught cleanly: the final frame is emitted once
and the function returns 0 with no traceback.
"""

from __future__ import annotations

import sys
import time
from typing import Callable

_INTERVAL_MIN = 1
_INTERVAL_MAX = 60

_fallback_warned = False
"""Module-level flag — ANSI fallback install hint is printed at most once per
process. Resolves review finding M1 (ambiguity: per-call vs per-process)."""


def clamp_interval(raw: int) -> int:
    """Clamp the user-supplied interval into the supported range.

    Out-of-range values emit a stderr warning and use the nearest boundary.
    """
    if raw < _INTERVAL_MIN:
        print(
            f"warning: --interval {raw} below minimum, using {_INTERVAL_MIN}",
            file=sys.stderr,
        )
        return _INTERVAL_MIN
    if raw > _INTERVAL_MAX:
        print(
            f"warning: --interval {raw} above maximum, using {_INTERVAL_MAX}",
            file=sys.stderr,
        )
        return _INTERVAL_MAX
    return raw


def _try_import_rich():
    try:
        from rich.live import Live  # type: ignore
        from rich.text import Text  # type: ignore

        return Live, Text
    except ImportError:
        return None


def _warn_fallback_once() -> None:
    global _fallback_warned
    if _fallback_warned:
        return
    print(
        "note: install awf-cli[tui] for richer rendering",
        file=sys.stderr,
    )
    _fallback_warned = True


def _run_rich(render_fn: Callable[[], str], interval: int, max_iters: int | None) -> int:
    imports = _try_import_rich()
    assert imports is not None  # caller verified
    Live, Text = imports
    last = ""
    iters = 0
    try:
        with Live(Text(""), refresh_per_second=4, transient=False) as live:
            while True:
                last = render_fn()
                live.update(Text(last))
                iters += 1
                if max_iters is not None and iters >= max_iters:
                    break
                time.sleep(interval)
    except KeyboardInterrupt:
        # Re-render last frame on stdout so the user sees the final state
        # after Live exits its alternate-screen-like context.
        if last:
            print(last)
        return 0
    return 0


def _run_ansi(render_fn: Callable[[], str], interval: int, max_iters: int | None) -> int:
    _warn_fallback_once()
    last = ""
    iters = 0
    try:
        while True:
            last = render_fn()
            # ESC[2J = clear screen, ESC[H = move cursor home.
            print("\x1b[2J\x1b[H" + last, flush=True)
            iters += 1
            if max_iters is not None and iters >= max_iters:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        if last:
            print(last)
        return 0
    return 0


def run_watch(
    render_fn: Callable[[], str],
    interval: int,
    *,
    max_iters: int | None = None,
) -> int:
    """Run a watch loop that re-renders ``render_fn()`` every ``interval`` seconds.

    Parameters
    ----------
    render_fn:
        Callable returning the text to display. Called once per iteration.
    interval:
        Sleep seconds between iterations. Already clamped by ``clamp_interval``;
        passed in unmodified here so the caller can log/decide.
    max_iters:
        Test-only stop condition. ``None`` means loop forever (production path).
        When set, the loop exits cleanly after N iterations without raising.

    Returns
    -------
    int
        Exit code (always 0 — graceful Ctrl+C or max_iters termination).
    """
    if _try_import_rich() is not None:
        return _run_rich(render_fn, interval, max_iters)
    return _run_ansi(render_fn, interval, max_iters)


def _reset_fallback_warned_for_tests() -> None:
    """Test helper — reset the process-wide warned flag between cases."""
    global _fallback_warned
    _fallback_warned = False
