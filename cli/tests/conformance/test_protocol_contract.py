"""Static contract checks for Provider Protocol (Phase B Step 4, T4).

These tests verify the shape of the public Provider contract without invoking
any LLM:
* ``ProviderResult`` exposes all seven contract fields (§3.2).
* ``ProviderCapability`` enum holds the nine documented members (§4.1).
* ``Provider.complete()`` keeps ``prompt`` positional while ``cwd`` / ``add_dirs``
  / ``timeout_sec`` are keyword-only (§3.2).
* Deprecated capabilities are clearly marked in the source file (§4.1).
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from awf.providers.base import Provider, ProviderCapability, ProviderResult
from awf.providers.fixture import FixtureProvider


EXPECTED_RESULT_FIELDS = {
    "returncode",
    "stdout",
    "stderr",
    "usage",
    "provider_name",
    "model",
    "elapsed_sec",
}

EXPECTED_CAPABILITY_NAMES = {
    "COMPLETE",
    "EVENT_STREAM",
    "TOOL_LOOP",
    "THINKING",
    "CITATIONS",
    "SESSION",
    "ADD_DIR",
    "ANALYZE_NATIVE",
    "WF_NATIVE",
}


def test_provider_result_has_all_fields() -> None:
    actual = {f.name for f in dataclasses.fields(ProviderResult)}
    assert actual == EXPECTED_RESULT_FIELDS, (
        f"ProviderResult fields mismatch. expected={EXPECTED_RESULT_FIELDS} actual={actual}"
    )


def test_capability_enum_complete() -> None:
    actual = {member.name for member in ProviderCapability}
    assert actual == EXPECTED_CAPABILITY_NAMES, (
        f"ProviderCapability members mismatch. expected={EXPECTED_CAPABILITY_NAMES} actual={actual}"
    )


def test_keyword_only_enforcement(tmp_path) -> None:
    sig = inspect.signature(Provider.complete)
    params = sig.parameters

    assert "prompt" in params, "Provider.complete must accept a 'prompt' parameter"
    assert params["prompt"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    for keyword_only in ("cwd", "add_dirs", "timeout_sec"):
        assert keyword_only in params, f"Provider.complete missing kw-only param '{keyword_only}'"
        assert params[keyword_only].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{keyword_only} must be KEYWORD_ONLY, got {params[keyword_only].kind}"
        )

    # Runtime check: passing a second positional arg must raise TypeError.
    fixture_file = tmp_path / "result.txt"
    fixture_file.write_text("ok\n", encoding="utf-8")
    provider = FixtureProvider(result_file=str(fixture_file))

    with pytest.raises(TypeError):
        provider.complete("hi", "/tmp/x")  # type: ignore[misc]


def test_deprecated_capabilities_marked() -> None:
    base_source = Path(__file__).resolve().parents[2] / "src" / "awf" / "providers" / "base.py"
    text = base_source.read_text(encoding="utf-8")

    lines = text.splitlines()
    analyze_line = next((ln for ln in lines if "ANALYZE_NATIVE" in ln and "=" in ln), None)
    wf_line = next((ln for ln in lines if "WF_NATIVE" in ln and "=" in ln), None)

    assert analyze_line is not None, "ANALYZE_NATIVE definition not found in base.py"
    assert wf_line is not None, "WF_NATIVE definition not found in base.py"
    assert "DEPRECATED" in analyze_line, f"ANALYZE_NATIVE must be marked DEPRECATED: {analyze_line!r}"
    assert "DEPRECATED" in wf_line, f"WF_NATIVE must be marked DEPRECATED: {wf_line!r}"
