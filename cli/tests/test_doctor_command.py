from __future__ import annotations

import argparse

from awf.commands import doctor as doctor_command


def test_run_doctor_describes_cmux_binary_without_active_run(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(doctor_command, "load_awf_config", lambda repo_root: object())
    monkeypatch.setattr(
        doctor_command,
        "collect_doctor_report",
        lambda config, repo_root, *, probe=False: {
            "default_provider": "fixture",
            "provider_fallback": [],
            "paths": {
                "repo_root": "/repo",
                "session_db": "/tmp/awf.db",
            },
            "mcp": {"server_count": 0, "servers": []},
            "dispatch": {
                "available_surfaces": ["inline"],
                "cmux_binary_on_path": True,
                "cmux_backend_ready": False,
                "pi_backend_ready": False,
                "surface_preference": {
                    "surface_preference": "auto",
                    "source": "default",
                },
                "surface_preference_ready": {
                    "status": "ok",
                    "detail": "auto dispatch can always fall back to inline",
                },
            },
            "runners": [],
            "pi_readiness": {},
            "providers": [],
        },
    )

    rc = doctor_command.run_doctor(
        argparse.Namespace(repo_root=".", json=False, probe=False, ci=False)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "cmux: binary on PATH but no active run" in out
    assert "not yet wired up" not in out
