"""watch_loop primitive tests (ATC-002/004/005/006/008/009)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — Python 3.9/3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from awf.core import watch_loop


@pytest.fixture(autouse=True)
def _reset_fallback_warned():
    watch_loop._reset_fallback_warned_for_tests()
    yield
    watch_loop._reset_fallback_warned_for_tests()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Replace time.sleep so the loop terminates instantly regardless of interval."""
    monkeypatch.setattr(watch_loop.time, "sleep", lambda *_a, **_k: None)


class TestClampInterval:
    """ATC-002 — interval clamp."""

    def test_below_min_clamps_to_one(self, capsys):
        assert watch_loop.clamp_interval(0) == 1
        assert "below minimum" in capsys.readouterr().err

    def test_negative_clamps_to_one(self, capsys):
        assert watch_loop.clamp_interval(-3) == 1
        assert "below minimum" in capsys.readouterr().err

    def test_at_min_no_warning(self, capsys):
        assert watch_loop.clamp_interval(1) == 1
        assert capsys.readouterr().err == ""

    def test_at_max_no_warning(self, capsys):
        assert watch_loop.clamp_interval(60) == 60
        assert capsys.readouterr().err == ""

    def test_above_max_clamps_to_sixty(self, capsys):
        assert watch_loop.clamp_interval(61) == 60
        assert "above maximum" in capsys.readouterr().err

    def test_well_above_max_clamps(self, capsys):
        assert watch_loop.clamp_interval(3600) == 60
        assert "above maximum" in capsys.readouterr().err


class TestAnsiFallback:
    """ATC-006 — when rich is unavailable, ANSI clear + one-shot install hint."""

    def test_ansi_fallback_clears_screen_and_warns_once(self, capsys):
        with patch.object(watch_loop, "_try_import_rich", return_value=None):
            rc = watch_loop.run_watch(lambda: "frame-A", interval=0, max_iters=1)
        assert rc == 0
        captured = capsys.readouterr()
        assert "\x1b[2J\x1b[H" in captured.out
        assert "frame-A" in captured.out
        assert "install awf-cli[tui]" in captured.err

    def test_fallback_hint_emitted_once_per_process(self, capsys):
        with patch.object(watch_loop, "_try_import_rich", return_value=None):
            watch_loop.run_watch(lambda: "x", interval=0, max_iters=1)
            first_err = capsys.readouterr().err
            assert first_err.count("install awf-cli[tui]") == 1

            watch_loop.run_watch(lambda: "y", interval=0, max_iters=1)
            second_err = capsys.readouterr().err
            assert "install awf-cli[tui]" not in second_err


class TestRichBranch:
    """ATC-005 — Rich Live used when import succeeds."""

    def test_rich_live_branch_invoked(self):
        fake_live_instance = MagicMock()
        fake_live_instance.__enter__ = MagicMock(return_value=fake_live_instance)
        fake_live_instance.__exit__ = MagicMock(return_value=False)
        fake_live_cls = MagicMock(return_value=fake_live_instance)
        fake_text_cls = MagicMock(side_effect=lambda s="": s)

        with patch.object(watch_loop, "_try_import_rich", return_value=(fake_live_cls, fake_text_cls)):
            rc = watch_loop.run_watch(lambda: "frame-B", interval=0, max_iters=1)

        assert rc == 0
        # Live was constructed and entered
        assert fake_live_cls.called
        assert fake_live_instance.__enter__.called
        # update was called with the rendered frame
        assert fake_live_instance.update.called
        # Text was constructed at least once with the rendered string
        assert any("frame-B" in str(call.args) for call in fake_text_cls.call_args_list)


class TestKeyboardInterrupt:
    """ATC-004 — Ctrl+C exits cleanly with last frame printed."""

    def test_ansi_keyboard_interrupt_returns_zero(self, capsys):
        seq = ["frame-1", "frame-2"]
        calls = {"n": 0}

        def render():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise KeyboardInterrupt
            return seq[0]

        with patch.object(watch_loop, "_try_import_rich", return_value=None):
            rc = watch_loop.run_watch(render, interval=0)
        assert rc == 0
        out = capsys.readouterr().out
        # The last successful frame is re-printed once after the interrupt.
        assert "frame-1" in out

    def test_rich_keyboard_interrupt_returns_zero(self, capsys):
        fake_live_instance = MagicMock()
        fake_live_instance.__enter__ = MagicMock(return_value=fake_live_instance)
        fake_live_instance.__exit__ = MagicMock(return_value=False)
        fake_live_cls = MagicMock(return_value=fake_live_instance)
        fake_text_cls = MagicMock(side_effect=lambda s="": s)

        calls = {"n": 0}

        def render():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise KeyboardInterrupt
            return "live-frame-1"

        with patch.object(watch_loop, "_try_import_rich", return_value=(fake_live_cls, fake_text_cls)):
            rc = watch_loop.run_watch(render, interval=0)
        assert rc == 0
        out = capsys.readouterr().out
        assert "live-frame-1" in out


class TestRenderEachIter:
    """ATC-008 — render_fn is called once per iteration."""

    def test_render_called_each_iter_ansi(self):
        calls = {"n": 0}

        def render():
            calls["n"] += 1
            return f"iter-{calls['n']}"

        with patch.object(watch_loop, "_try_import_rich", return_value=None):
            rc = watch_loop.run_watch(render, interval=0, max_iters=3)
        assert rc == 0
        assert calls["n"] == 3

    def test_render_called_each_iter_rich(self):
        fake_live_instance = MagicMock()
        fake_live_instance.__enter__ = MagicMock(return_value=fake_live_instance)
        fake_live_instance.__exit__ = MagicMock(return_value=False)
        fake_live_cls = MagicMock(return_value=fake_live_instance)
        fake_text_cls = MagicMock(side_effect=lambda s="": s)

        calls = {"n": 0}

        def render():
            calls["n"] += 1
            return f"iter-{calls['n']}"

        with patch.object(watch_loop, "_try_import_rich", return_value=(fake_live_cls, fake_text_cls)):
            rc = watch_loop.run_watch(render, interval=0, max_iters=4)
        assert rc == 0
        assert calls["n"] == 4


class TestPyprojectTuiExtra:
    """ATC-009 — pyproject extras define tui = [rich>=13.0.0]."""

    def test_tui_extra_defines_rich(self):
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        extras = data["project"]["optional-dependencies"]
        assert "tui" in extras
        assert any(dep.startswith("rich") for dep in extras["tui"])
        assert any("rich>=13" in dep for dep in extras["tui"])
