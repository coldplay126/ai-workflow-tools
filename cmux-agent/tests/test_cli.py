"""CLI 테스트."""

import sys
from unittest.mock import patch

import pytest

from cmux_agent.cli import main
from cmux_agent.cli.commands import (
    SUPPORTED_COMMANDS,
    _is_current_python_env_script,
    _missing_supported_commands,
    _parse_help_commands,
)


class TestCLI:
    def test_no_command_defaults_to_start(self):
        """인자 없이 실행하면 start가 기본 동작."""
        from unittest.mock import patch
        with patch("cmux_agent.cli.cmd_start") as mock_start:
            main([])
            mock_start.assert_called_once()

    def test_doctor(self, capsys):
        main(["doctor"])
        output = capsys.readouterr().out
        assert "python" in output
        assert "cmux-agent module" in output
        assert "cmux-agent supported commands" in output

    def test_parse_help_commands(self):
        output = """
        usage: cmux-agent [-h]
                          {doctor,start,task,stop,register,agents,watch,status,events,send,messages} ...
        """

        commands = _parse_help_commands(output)

        assert "doctor" in commands
        assert "start" in commands
        assert "messages" in commands

    def test_missing_supported_commands_detects_stale_cli(self):
        stale_commands = set(SUPPORTED_COMMANDS) - {"spawn", "failures"}

        assert _missing_supported_commands(stale_commands) == ["spawn", "failures"]

    def test_current_python_env_script_allows_venv_python_symlinks(self, tmp_path):
        bin_dir = tmp_path / "env" / "bin"
        bin_dir.mkdir(parents=True)
        python = bin_dir / "python"
        script = bin_dir / "cmux-agent"
        python.write_text("#!/bin/sh\n")
        script.write_text("#!/bin/sh\n")

        assert _is_current_python_env_script(script, executable=str(python))

    def test_unknown_command(self, capsys):
        with pytest.raises(SystemExit):
            main(["nonexistent"])
