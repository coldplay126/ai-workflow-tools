from __future__ import annotations

import argparse

from awf.commands import doctor as doctor_command


def _base_doctor_payload() -> dict:
    return {
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
    }


def test_run_doctor_describes_cmux_binary_without_active_run(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(doctor_command, "load_awf_config", lambda repo_root: object())
    monkeypatch.setattr(
        doctor_command,
        "collect_doctor_report",
        lambda config, repo_root, *, probe=False: _base_doctor_payload(),
    )

    rc = doctor_command.run_doctor(
        argparse.Namespace(repo_root=".", json=False, probe=False, ci=False)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "cmux: binary on PATH but no active run" in out
    assert "not yet wired up" not in out


def test_run_doctor_prints_latest_pi_field_smoke(monkeypatch, capsys) -> None:
    payload = _base_doctor_payload()
    payload["pi_readiness"] = {
        "status": "ready",
        "dispatch_surface": "opt_in_only",
        "command": "pi",
        "command_source": "default",
        "path": "/bin/pi",
        "version": {"status": "ok", "detail": "0.0.0"},
        "auth_env_present": {},
        "billing_warning": {"status": "caution", "detail": "billing warning"},
        "field_smoke_command": "python3 cli/tests/run_pi_field_smoke.py --json",
        "last_field_smoke": {
            "status": "found",
            "ok": False,
            "reason": "provider_quota_exhausted",
            "recorded_at": "2026-05-10T00:00:00+00:00",
        },
    }
    monkeypatch.setattr(doctor_command, "load_awf_config", lambda repo_root: object())
    monkeypatch.setattr(
        doctor_command,
        "collect_doctor_report",
        lambda config, repo_root, *, probe=False: payload,
    )

    rc = doctor_command.run_doctor(
        argparse.Namespace(repo_root=".", json=False, probe=False, ci=False)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "last_field_smoke: ok=False reason=provider_quota_exhausted" in out
