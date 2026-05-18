"""`awf dashboard` MVP tests (D2) — ATC-002/003/004/005/006/007/008."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from awf.core import dashboard

# rich is an optional extras (`awf-cli[tui]`) but tests that exercise rich.Live /
# Panel / Layout require it. CI environments without rich auto-skip those classes
# instead of erroring out. rich import 분기 자체(TestRichImportFailure)는 rich
# 미설치도 검증해야 하므로 module-level importorskip은 사용하지 않고 개별 클래스에
# skipif marker 를 적용한다.
_RICH_AVAILABLE = dashboard._try_import_rich() is not None
_requires_rich = pytest.mark.skipif(
    not _RICH_AVAILABLE,
    reason="requires rich (awf-cli[tui]) — install with `pip install rich>=13.0.0`",
)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Bypass time.sleep so event loop terminates instantly in tests."""
    monkeypatch.setattr(dashboard.time, "sleep", lambda *_a, **_k: None)


class FakeKeyReader:
    """Test KeyReader — returns a pre-seeded sequence of keys, then None forever."""

    def __init__(self, seq: list[Optional[str]]):
        self._seq = list(seq)
        self.calls = 0

    def __enter__(self) -> "FakeKeyReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read_nonblocking(self, timeout: float) -> Optional[str]:
        self.calls += 1
        if self._seq:
            return self._seq.pop(0)
        return None


class RaiseKeyboardInterruptReader:
    """Test KeyReader — raises KeyboardInterrupt on first read."""

    def __enter__(self) -> "RaiseKeyboardInterruptReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read_nonblocking(self, timeout: float) -> Optional[str]:
        raise KeyboardInterrupt


class TestRichImportFailure:
    """ATC-003 — rich 미설치 시 stderr 안내 + return 2."""

    def test_rich_import_failure(self, capsys):
        with patch.object(dashboard, "_try_import_rich", return_value=None):
            rc = dashboard.run_dashboard(None, 5)
        assert rc == 2
        err = capsys.readouterr().err
        assert "awf dashboard requires the rich library" in err
        assert "pip install 'awf-cli[tui]'" in err
        assert "uv tool install --with 'rich>=13.0.0' awf-cli" in err


class TestIntervalClampReuse:
    """ATC-002 — D1 clamp_interval 재사용 (0→1, 100→60)."""

    def test_interval_below_min(self, capsys):
        assert dashboard.clamp_interval(0) == 1
        assert "below minimum" in capsys.readouterr().err

    def test_interval_above_max(self, capsys):
        assert dashboard.clamp_interval(120) == 60
        assert "above maximum" in capsys.readouterr().err

    def test_interval_in_range(self, capsys):
        assert dashboard.clamp_interval(7) == 7
        assert capsys.readouterr().err == ""


@_requires_rich
class TestKeyBindings:
    """ATC-006/007/008 — q quit / Ctrl+C / r force refresh."""

    def test_q_key_quits(self, tmp_path: Path):
        reader = FakeKeyReader(["q"])
        rc = dashboard.run_dashboard(str(tmp_path), interval=60, key_reader=reader)
        assert rc == 0

    def test_upper_q_also_quits(self, tmp_path: Path):
        reader = FakeKeyReader(["Q"])
        rc = dashboard.run_dashboard(str(tmp_path), interval=60, key_reader=reader)
        assert rc == 0

    def test_keyboard_interrupt(self, tmp_path: Path):
        rc = dashboard.run_dashboard(
            str(tmp_path), interval=60, key_reader=RaiseKeyboardInterruptReader(),
        )
        assert rc == 0

    def test_r_key_forces_refresh(self, tmp_path: Path):
        """r 키 직후 즉시 재렌더 + elapsed=0 리셋 (M1 해소)."""
        reader = FakeKeyReader(["r", "q"])
        call_count = {"n": 0}

        original_render = dashboard.render_workflow_panel

        def counting_render(*args, **kwargs):
            call_count["n"] += 1
            return original_render(*args, **kwargs)

        with patch.object(dashboard, "render_workflow_panel", side_effect=counting_render):
            rc = dashboard.run_dashboard(str(tmp_path), interval=60, key_reader=reader)
        assert rc == 0
        # 초기 refresh_panels (1) + r 키 직후 refresh (1) = 2회 이상.
        # interval=60 + monkeypatched sleep 으로 자동 refresh 는 일어나지 않아야 한다.
        assert call_count["n"] >= 2

    def test_unrecognized_key_continues_loop(self, tmp_path: Path):
        """모르는 키는 loop 유지, 그 다음 q 로 종료."""
        reader = FakeKeyReader(["x", "q"])
        rc = dashboard.run_dashboard(str(tmp_path), interval=60, key_reader=reader)
        assert rc == 0


@_requires_rich
class TestAutoRefresh:
    """interval 도달 시 자동 refresh — max_iters 로 안전 종료."""

    def test_auto_refresh_after_interval(self, tmp_path: Path):
        # interval=1 + sleep 무시 + max_iters=2 → 자동 refresh 2회 후 종료
        reader = FakeKeyReader([None] * 100)
        call_count = {"n": 0}

        original_render = dashboard.render_workflow_panel

        def counting_render(*args, **kwargs):
            call_count["n"] += 1
            return original_render(*args, **kwargs)

        with patch.object(dashboard, "render_workflow_panel", side_effect=counting_render):
            rc = dashboard.run_dashboard(
                str(tmp_path), interval=1, key_reader=reader, max_iters=2,
            )
        assert rc == 0
        # 초기 refresh + 2회 자동 refresh = 3회 이상
        assert call_count["n"] >= 3


@_requires_rich
class TestPanelRendering:
    """ATC-004 / ATC-005 — workflow + broker panel 렌더."""

    def test_workflow_panel_with_state(self, tmp_path: Path):
        imports = dashboard._try_import_rich()
        assert imports is not None, "rich must be installed for this test"
        _, _, Panel, Text = imports
        # 가상의 workflow_state 반환
        fake_state = {
            "id": "test-cycle",
            "currentPhase": "impl",
            "phases": {"plan": {"status": "completed"}, "impl": {"status": "in_progress"}},
        }
        with patch("awf.core.dashboard.load_workflow_state", return_value=fake_state):
            panel = dashboard.render_workflow_panel(str(tmp_path), Panel=Panel, Text=Text)
        assert panel.title == "Workflow"
        # Panel content 안에 'current_phase' 같은 summarize 출력이 포함되어야 한다
        content = str(panel.renderable)
        assert "test-cycle" in content or "impl" in content

    def test_workflow_panel_when_state_missing(self, tmp_path: Path):
        imports = dashboard._try_import_rich()
        assert imports is not None
        _, _, Panel, Text = imports
        with patch("awf.core.dashboard.load_workflow_state", side_effect=FileNotFoundError("no state")):
            panel = dashboard.render_workflow_panel(str(tmp_path), Panel=Panel, Text=Text)
        assert panel.title == "Workflow"
        content = str(panel.renderable)
        assert "no workflow state" in content

    def test_broker_panel_alive(self, tmp_path: Path):
        imports = dashboard._try_import_rich()
        assert imports is not None
        _, _, Panel, Text = imports
        fake_health = {
            "broker_daemon": {"status": "alive", "pid": 12345},
            "events_log": {"status": "fresh"},
            "sqlite_integrity": {"status": "ok"},
        }
        with patch("awf.core.dashboard.probe_cmux_broker_health", return_value=fake_health):
            panel = dashboard.render_broker_panel(str(tmp_path), Panel=Panel, Text=Text)
        assert panel.title == "Cmux Broker Health"
        content = str(panel.renderable)
        assert "alive" in content
        assert "12345" in content
        assert "fresh" in content
        assert "ok" in content

    def test_broker_panel_absent(self, tmp_path: Path):
        imports = dashboard._try_import_rich()
        assert imports is not None
        _, _, Panel, Text = imports
        fake_health = {
            "broker_daemon": {"status": "absent", "pid": None},
            "events_log": {"status": "missing"},
            "sqlite_integrity": {"status": "skip"},
        }
        with patch("awf.core.dashboard.probe_cmux_broker_health", return_value=fake_health):
            panel = dashboard.render_broker_panel(str(tmp_path), Panel=Panel, Text=Text)
        content = str(panel.renderable)
        assert "absent" in content
        assert "missing" in content

    def test_broker_panel_handles_empty_health(self, tmp_path: Path):
        imports = dashboard._try_import_rich()
        assert imports is not None
        _, _, Panel, Text = imports
        with patch("awf.core.dashboard.probe_cmux_broker_health", return_value={}):
            panel = dashboard.render_broker_panel(str(tmp_path), Panel=Panel, Text=Text)
        content = str(panel.renderable)
        # 빈 dict 일 때 status/pid 등이 "-" 로 표기되어야 한다
        assert "Status" in content
        assert "PID" in content


class TestStdinKeyReaderFallback:
    """StdinKeyReader — non-tty 환경에서 graceful fallback."""

    def test_non_tty_does_not_raise(self):
        reader = dashboard.StdinKeyReader()
        with patch.object(sys.stdin, "isatty", return_value=False):
            with reader:
                # non-tty 에서는 fd 가 None 으로 유지되고, read_nonblocking 은 None 반환
                assert reader._fd is None
                assert reader.read_nonblocking(0.01) is None
