from __future__ import annotations

from awf.core.config import AwfConfig
from awf.core.readiness import check_provider_readiness
from awf.providers.gemini import GeminiProvider
from awf.providers.registry import ProviderRegistry


def test_registry_creates_gemini_provider_with_auto_model() -> None:
    config = AwfConfig.defaults().merge(
        {
            "provider": {
                "default": "gemini",
                "gemini": {
                    "command": "gemini",
                    "flags": ["--output-format", "text"],
                    "model": "",
                },
            }
        }
    )

    provider = ProviderRegistry(config).get("gemini")

    assert isinstance(provider, GeminiProvider)
    assert provider.model == ""


def test_gemini_readiness_reports_auto_model(monkeypatch) -> None:
    monkeypatch.setattr("awf.core.readiness.shutil.which", lambda command: f"/bin/{command}")
    config = AwfConfig.defaults().merge(
        {
            "provider": {
                "gemini": {
                    "command": "gemini",
                    "model": "",
                }
            }
        }
    )

    status = check_provider_readiness("gemini", config)

    assert status["installed"]["status"] == "ok"
    assert status["configured"]["status"] == "skip"
    assert status["configured"]["model"] == "auto"
