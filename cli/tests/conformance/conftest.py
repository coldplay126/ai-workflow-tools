"""Shared fixtures for provider conformance tests (Phase B Step 4).

These fixtures give each Tier 0 test a freshly-built provider instance plus a
known-good path to the baseline fixture response file. Live-provider fixtures
are parametrized with ``pytest.mark.live`` so they are deselected by default
(see ``pyproject.toml`` ``addopts``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.providers.fixture import FixtureProvider


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def baseline_fixture_path() -> Path:
    """Absolute path to the canonical baseline fixture response file."""
    path = FIXTURES_DIR / "fixture_echo.txt"
    assert path.is_file(), f"baseline fixture missing: {path}"
    return path


@pytest.fixture()
def fixture_provider(baseline_fixture_path: Path) -> FixtureProvider:
    """FixtureProvider instance backed by the baseline echo fixture."""
    return FixtureProvider(result_file=str(baseline_fixture_path))


@pytest.fixture(
    params=[
        pytest.param("claude-code", marks=pytest.mark.live),
        pytest.param("claude-sdk", marks=pytest.mark.live),
        pytest.param("codex", marks=pytest.mark.live),
        pytest.param("openai", marks=pytest.mark.live),
    ]
)
def live_provider_name(request: pytest.FixtureRequest) -> str:
    """Parametrized provider name for live (real-CLI/API) conformance tests."""
    return str(request.param)
