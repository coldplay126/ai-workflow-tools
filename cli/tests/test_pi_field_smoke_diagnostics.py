from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_smoke_module() -> ModuleType:
    script_path = Path(__file__).resolve().parent / "run_pi_field_smoke.py"
    spec = importlib.util.spec_from_file_location("run_pi_field_smoke", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SMOKE = _load_smoke_module()


def test_diagnose_dispatch_classifies_anthropic_extra_usage() -> None:
    diagnosis = SMOKE._diagnose_dispatch(
        {
            "ok": False,
            "returncode": 1,
            "timed_out": False,
            "parse_error": False,
            "stderr_preview": (
                "400 {\"type\":\"error\",\"error\":{\"message\":"
                "\"You're out of extra usage. Add more at "
                "claude.ai/settings/usage and keep going.\"}}"
            ),
        }
    )

    assert diagnosis["kind"] == "provider_quota_exhausted"
    assert diagnosis["billing_context"] == "anthropic_extra_usage"
    assert "Extra Usage" in diagnosis["summary"]


def test_diagnose_dispatch_classifies_missing_provider_auth() -> None:
    diagnosis = SMOKE._diagnose_dispatch(
        {
            "ok": False,
            "returncode": 1,
            "timed_out": False,
            "parse_error": False,
            "stderr_preview": "No API key found for provider anthropic.",
        }
    )

    assert diagnosis["kind"] == "missing_provider_auth"


def test_diagnose_dispatch_classifies_contract_parse_error() -> None:
    diagnosis = SMOKE._diagnose_dispatch(
        {
            "ok": False,
            "returncode": 0,
            "timed_out": False,
            "parse_error": True,
            "stdout_preview": "I think it passed.",
            "stderr_preview": "",
        }
    )

    assert diagnosis["kind"] == "provider_contract_parse_error"


def test_set_diagnosis_promotes_machine_reason_and_next_action() -> None:
    payload: dict[str, object] = {}
    SMOKE._set_diagnosis(
        payload,
        {
            "kind": "provider_quota_exhausted",
            "summary": "Quota exhausted.",
            "next_action": "Add usage.",
            "billing_context": "anthropic_extra_usage",
        },
    )

    assert payload["reason"] == "provider_quota_exhausted"
    assert payload["next_action"] == "Add usage."
    assert payload["billing_context"] == "anthropic_extra_usage"


def test_main_can_write_latest_result_without_real_provider(
    tmp_path: Path,
    capsys,
) -> None:
    rc = SMOKE.main(
        [
            "--pi-command",
            str(tmp_path / "missing-pi"),
            "--repo-root",
            str(tmp_path),
            "--write-result",
            "--json",
        ]
    )

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "pi_not_found"
    result_path = Path(payload["result_path"])
    assert result_path == tmp_path / ".awf-operations" / "pi-field-smoke" / "latest.json"
    envelope = json.loads(result_path.read_text(encoding="utf-8"))
    assert envelope["schema"] == "awf_pi_field_smoke_latest_v1"
    assert envelope["payload"]["reason"] == "pi_not_found"
