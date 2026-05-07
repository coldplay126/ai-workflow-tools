"""Live SDK conformance skeletons for claude-sdk / openai (Phase B Step 4, T9).

All tests here are guarded by ``@pytest.mark.live`` and are deselected in the
default ``pytest`` invocation. CI jobs that want to run them opt in with
``pytest -m live``.

SDK-backed providers do not yet honour ``timeout_sec`` end-to-end. The timeout
test is therefore marked ``xfail(strict=False)`` so it surfaces progress once
Step 2 adds full enforcement without failing the baseline suite today.
"""

from __future__ import annotations

import os

import pytest

from awf.providers.base import ProviderResult


API_KEY_ENV = {
    "claude-sdk": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@pytest.mark.live
@pytest.mark.parametrize("provider_name", ["claude-sdk", "openai"])
def test_live_sdk_complete_returns_result(provider_name: str) -> None:
    _require_api_key(provider_name)

    registry, _config = _build_live_registry()
    try:
        provider = registry.get(provider_name)
    except Exception as exc:  # pragma: no cover - live setup dependent
        pytest.skip(f"{provider_name} provider unavailable: {exc}")

    result = provider.complete("say hi in one word")
    assert isinstance(result, ProviderResult)
    assert result.returncode == 0
    assert result.usage is not None, "SDK providers should report usage"


@pytest.mark.live
@pytest.mark.xfail(
    reason="SDK providers do not yet honour timeout_sec fully (Step 2 scope)",
    strict=False,
)
@pytest.mark.parametrize("provider_name", ["claude-sdk", "openai"])
def test_live_sdk_timeout_marked_xfail(provider_name: str) -> None:
    _require_api_key(provider_name)

    registry, _config = _build_live_registry()
    try:
        provider = registry.get(provider_name)
    except Exception as exc:  # pragma: no cover - live setup dependent
        pytest.skip(f"{provider_name} provider unavailable: {exc}")

    result = provider.complete(
        "write a detailed 2000 word essay about black holes",
        timeout_sec=1,
    )
    # Once SDK timeout enforcement lands, this should hold.
    assert result.returncode == 124
    assert result.stderr.startswith("provider_timeout:")


@pytest.mark.live
@pytest.mark.parametrize("provider_name", ["claude-sdk", "openai"])
def test_live_sdk_unicode_roundtrip(provider_name: str) -> None:
    _require_api_key(provider_name)

    registry, _config = _build_live_registry()
    try:
        provider = registry.get(provider_name)
    except Exception as exc:  # pragma: no cover - live setup dependent
        pytest.skip(f"{provider_name} provider unavailable: {exc}")

    result = provider.complete("한글과 이모지 🚀 를 그대로 보내세요")
    assert isinstance(result, ProviderResult)


def _require_api_key(provider_name: str) -> None:
    env_name = API_KEY_ENV.get(provider_name)
    if env_name and not os.environ.get(env_name):
        pytest.skip(f"API key missing: {env_name}")


def _build_live_registry():
    """Construct a ProviderRegistry backed by an empty AwfConfig.

    Note: `AwfConfig({})` is intentional — SDK live tests rely on vendor
    env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) rather than AwfConfig
    fields. `_require_api_key()` guards the tests when those env vars are
    missing.
    """

    from awf.core.config import AwfConfig
    from awf.providers.registry import ProviderRegistry

    config = AwfConfig({})
    registry = ProviderRegistry(config)
    return registry, config
