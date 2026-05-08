"""Tests for cmux-agent doctor's script-probe failure classification."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cmux_agent.cli.commands import (
    PROBE_MODULE_NOT_FOUND,
    PROBE_NONZERO_EXIT,
    PROBE_OK,
    PROBE_OS_ERROR,
    PROBE_TIMEOUT,
    _probe_cmux_agent_script,
)


def _make_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_probe_returns_ok_for_help_script(tmp_path: Path):
    script = _make_executable(
        tmp_path / "fake-cmux-agent",
        "#!/bin/bash\n"
        "cat <<'EOF'\n"
        "usage: cmux-agent [-h] {doctor,start,task,stop,register,spawn,agents,watch,"
        "status,events,failures,send,messages} ...\n"
        "EOF\n",
    )

    result = _probe_cmux_agent_script(script)
    assert result["status"] == PROBE_OK
    assert result["commands"] is not None
    assert "doctor" in result["commands"]


def test_probe_detects_module_not_found(tmp_path: Path):
    # Simulate the Py 3.13 + uv editable install symptom: exits non-zero
    # with a Python traceback that mentions cmux_agent.
    script = _make_executable(
        tmp_path / "broken-cmux-agent",
        "#!/bin/bash\n"
        "cat <<'EOF' >&2\n"
        "Traceback (most recent call last):\n"
        '  File "/x/.venv/bin/cmux-agent", line 4, in <module>\n'
        "    from cmux_agent.cli import main\n"
        "ModuleNotFoundError: No module named 'cmux_agent'\n"
        "EOF\n"
        "exit 1\n",
    )

    result = _probe_cmux_agent_script(script)
    assert result["status"] == PROBE_MODULE_NOT_FOUND
    assert result["commands"] is None
    assert "cmux_agent" in result["stderr"]
    assert result["returncode"] == 1


def test_probe_module_not_found_takes_precedence_over_nonzero_exit(tmp_path: Path):
    # Even when both signals are present, ModuleNotFoundError must win so
    # the doctor message points operators at the right remediation.
    script = _make_executable(
        tmp_path / "mixed-broken",
        "#!/bin/bash\n"
        "cat <<'EOF' >&2\n"
        "ModuleNotFoundError: No module named 'cmux_agent'\n"
        "EOF\n"
        "exit 2\n",
    )
    result = _probe_cmux_agent_script(script)
    assert result["status"] == PROBE_MODULE_NOT_FOUND


def test_probe_reports_nonzero_exit_when_no_module_signal(tmp_path: Path):
    script = _make_executable(
        tmp_path / "bad-exit",
        "#!/bin/bash\n"
        "echo 'something else' >&2\n"
        "exit 2\n",
    )
    result = _probe_cmux_agent_script(script)
    assert result["status"] == PROBE_NONZERO_EXIT
    assert result["returncode"] == 2
    assert "something else" in result["stderr"]


def test_probe_handles_missing_executable(tmp_path: Path):
    result = _probe_cmux_agent_script(tmp_path / "does-not-exist")
    # Subprocess raises FileNotFoundError → OSError category.
    assert result["status"] == PROBE_OS_ERROR
    assert result["commands"] is None


def test_probe_handles_timeout(tmp_path: Path, monkeypatch):
    import subprocess as _subprocess

    def fake_run(*_args, **_kwargs):
        raise _subprocess.TimeoutExpired(cmd="cmux-agent", timeout=5)

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(
        "cmux_agent.cli.commands.subprocess",
        _subprocess,
    )

    result = _probe_cmux_agent_script(tmp_path / "anything")
    assert result["status"] == PROBE_TIMEOUT
    assert result["returncode"] == -1
