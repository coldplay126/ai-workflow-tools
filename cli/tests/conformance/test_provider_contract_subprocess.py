"""Tier 0 tests targeting SubprocessProvider + live CLI providers.

T7 uses ``unittest.mock.patch`` on the module-level ``subprocess.run`` reference
inside ``awf.providers.subprocess_provider`` so that timeout + error paths are
verified deterministically (no real process, no sleep).

T8 adds ``@pytest.mark.live`` skeletons for the two CLI-based providers
(``claude-code``, ``codex``). These are deselected by default via the
``addopts = "-m 'not live'"`` setting in ``pyproject.toml``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from awf.providers.base import ProviderResult
from awf.providers.subprocess_provider import SubprocessProvider


# -- T7: deterministic mock-based checks --------------------------------------


def test_subprocess_timeout_returns_124() -> None:
    provider = SubprocessProvider(command="fake", flags=[])

    timeout_error = subprocess.TimeoutExpired(cmd=["fake"], timeout=1)
    with mock.patch(
        "awf.providers.subprocess_provider.subprocess.run",
        side_effect=timeout_error,
    ):
        result = provider.complete("hello", timeout_sec=1)

    assert isinstance(result, ProviderResult)
    assert result.returncode == 124
    assert result.stderr.startswith("provider_timeout:")


def test_subprocess_timeout_none_uses_no_timeout() -> None:
    provider = SubprocessProvider(command="fake", flags=[])

    completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
    with mock.patch(
        "awf.providers.subprocess_provider.subprocess.run",
        return_value=completed,
    ) as run_mock:
        provider.complete("hello", timeout_sec=None)

    assert run_mock.call_count == 1
    assert run_mock.call_args.kwargs["timeout"] is None


def test_subprocess_returns_structured_error() -> None:
    provider = SubprocessProvider(command="fake", flags=[])

    completed = SimpleNamespace(returncode=1, stdout="", stderr="boom")
    with mock.patch(
        "awf.providers.subprocess_provider.subprocess.run",
        return_value=completed,
    ):
        result = provider.complete("hello")

    assert isinstance(result, ProviderResult)
    assert result.returncode == 1
    assert result.stderr == "boom"


# -- T8: live CLI provider skeletons (skipped by default) ---------------------


@pytest.mark.live
@pytest.mark.parametrize("provider_name", ["claude-code", "codex"])
def test_live_complete_returns_result(provider_name: str) -> None:
    registry, _config = _build_live_registry()
    try:
        provider = registry.get(provider_name)
    except Exception as exc:  # pragma: no cover - live setup dependent
        pytest.skip(f"{provider_name} provider unavailable: {exc}")

    result = provider.complete("return OK")
    assert isinstance(result, ProviderResult)
    assert result.returncode == 0
    assert result.stdout.strip() != ""


@pytest.mark.live
@pytest.mark.parametrize("provider_name", ["claude-code", "codex"])
def test_live_unicode_roundtrip(provider_name: str) -> None:
    registry, _config = _build_live_registry()
    try:
        provider = registry.get(provider_name)
    except Exception as exc:  # pragma: no cover - live setup dependent
        pytest.skip(f"{provider_name} provider unavailable: {exc}")

    result = provider.complete("한글과 이모지 🚀 를 그대로 보내세요")
    # We only assert no exception + structured result here; actual content
    # varies across providers/models.
    assert isinstance(result, ProviderResult)


def _build_live_registry():
    """Construct a ProviderRegistry backed by an empty AwfConfig.

    Isolated in a helper so the import cost is paid only when live tests run.

    Note: `AwfConfig({})` is an intentional bare config — live tests rely on
    each provider's built-in defaults (env vars like `AWF_CLAUDE_TIMEOUT_SEC`,
    CLI PATH discovery). Tests that need richer config should skip or extend
    this helper rather than mutate the shared one.
    """

    from awf.core.config import AwfConfig
    from awf.providers.registry import ProviderRegistry

    config = AwfConfig({})
    registry = ProviderRegistry(config)
    return registry, config
