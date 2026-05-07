"""Tier 0 MUST conformance tests against FixtureProvider (Phase B Step 4, T5/T6).

FixtureProvider is a deterministic local provider that reads a result file and
returns it as stdout. It supports a handful of environment-variable toggles
(``AWF_FIXTURE_RETURNCODE``, ``AWF_FIXTURE_USAGE_JSON``) which we use here to
exercise the structured error + usage paths required by §9.1.

These tests exercise the five Tier 0 MUST cases listed in the provider contract
(§9.1). Real-provider coverage lives in ``test_tier0_subprocess.py`` and
``test_tier0_sdk.py`` under ``@pytest.mark.live``.
"""

from __future__ import annotations

import pytest

from awf.providers.base import ProviderResult, TokenUsage
from awf.providers.fixture import FixtureProvider


# -- T5 -----------------------------------------------------------------------


def test_complete_returns_result(
    fixture_provider: FixtureProvider, baseline_fixture_path
) -> None:
    result = fixture_provider.complete("ping")

    assert isinstance(result, ProviderResult)
    assert result.returncode == 0
    expected_body = baseline_fixture_path.read_text(encoding="utf-8")
    assert expected_body.strip() in result.stdout

    # All seven fields must be accessible without AttributeError.
    assert hasattr(result, "usage")
    assert hasattr(result, "provider_name")
    assert hasattr(result, "model")
    assert hasattr(result, "elapsed_sec")


def test_unicode_roundtrip(fixture_provider: FixtureProvider) -> None:
    # FixtureProvider does not echo the prompt; the objective here is to
    # confirm that a unicode prompt does not raise UnicodeEncodeError and that
    # the fixture body itself (which contains Korean + emoji) round-trips.
    result = fixture_provider.complete("안녕하세요 🚀")

    assert isinstance(result, ProviderResult)
    assert result.returncode == 0
    assert "안녕하세요" in result.stdout
    assert "🚀" in result.stdout


# -- T6 -----------------------------------------------------------------------


def test_error_is_structured(
    fixture_provider: FixtureProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWF_FIXTURE_RETURNCODE", "2")

    result = fixture_provider.complete("trigger error")

    assert isinstance(result, ProviderResult)
    assert result.returncode == 2  # structured error, no exception raised.


def test_timeout_enforced_fixture(fixture_provider: FixtureProvider) -> None:
    # FixtureProvider returns immediately, but must accept timeout_sec as a
    # keyword-only argument per §3.2 without raising.
    result = fixture_provider.complete("ping", timeout_sec=1)

    assert isinstance(result, ProviderResult)
    assert result.returncode == 0


def test_usage_reported_with_env(
    fixture_provider: FixtureProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "AWF_FIXTURE_USAGE_JSON", '{"input_tokens": 10, "output_tokens": 5}'
    )

    result = fixture_provider.complete("count tokens")

    assert isinstance(result.usage, TokenUsage)
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5


def test_usage_reported_without_env(
    fixture_provider: FixtureProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWF_FIXTURE_USAGE_JSON", raising=False)

    result = fixture_provider.complete("no usage")

    if result.usage is None:
        pytest.skip("fixture does not emit usage by default")
    # If a future fixture version does report usage without the env var, the
    # reported numbers must still be non-negative.
    assert result.usage.input_tokens >= 0
    assert result.usage.output_tokens >= 0
