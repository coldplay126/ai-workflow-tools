from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Mapping

import pytest

from awf.core.config import AwfConfig
from awf.supervisor.config import SupervisorConfig, load_supervisor_config


_SUPERVISOR_DEFAULTS = {
    "api_url": "",
    "region": "ap-northeast-2",
    "profile": "",
    "poll_interval_seconds": 2,
    "request_timeout_seconds": 30,
}


def _write_supervisor_config(path: Path, values: Mapping[str, Any]) -> None:
    lines = ["[supervisor]"]
    lines.extend("{} = {}".format(key, json.dumps(value)) for key, value in values.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _isolated_config_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".awf.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AWF_SUPERVISOR_API_URL", raising=False)
    monkeypatch.delenv("AWF_SUPERVISOR_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.chdir(repo)
    return home, repo


def _config_with(**overrides: Any) -> SupervisorConfig:
    values = dict(_SUPERVISOR_DEFAULTS)
    values["api_url"] = "https://abc123.execute-api.ap-northeast-2.amazonaws.com"
    values.update(overrides)
    return SupervisorConfig(**values)


def test_awf_config_exposes_exact_supervisor_defaults() -> None:
    assert AwfConfig.defaults().raw["supervisor"] == _SUPERVISOR_DEFAULTS


def test_load_supervisor_config_uses_exact_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_config_paths(monkeypatch, tmp_path)

    assert load_supervisor_config() == SupervisorConfig(**_SUPERVISOR_DEFAULTS)


def test_load_supervisor_config_reads_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, _ = _isolated_config_paths(monkeypatch, tmp_path)
    _write_supervisor_config(
        home / ".config" / "awf" / "config.toml",
        {
            "api_url": "https://abc123.execute-api.us-east-1.amazonaws.com",
            "region": "us-east-1",
            "profile": "user-profile",
            "poll_interval_seconds": 7,
            "request_timeout_seconds": 45,
        },
    )

    config = load_supervisor_config()

    assert config == SupervisorConfig(
        api_url="https://abc123.execute-api.us-east-1.amazonaws.com",
        region="us-east-1",
        profile="user-profile",
        poll_interval_seconds=7,
        request_timeout_seconds=45,
    )


def test_load_supervisor_config_prefers_project_config_over_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, repo = _isolated_config_paths(monkeypatch, tmp_path)
    _write_supervisor_config(
        home / ".config" / "awf" / "config.toml",
        {
            "api_url": "https://abc123.execute-api.us-east-1.amazonaws.com",
            "region": "us-east-1",
            "profile": "user-profile",
            "poll_interval_seconds": 7,
            "request_timeout_seconds": 45,
        },
    )
    _write_supervisor_config(
        repo / ".awf.toml",
        {
            "api_url": "https://abc123.execute-api.eu-west-1.amazonaws.com",
            "region": "eu-west-1",
            "profile": "project-profile",
            "poll_interval_seconds": 3,
            "request_timeout_seconds": 20,
        },
    )

    config = load_supervisor_config()

    assert config == SupervisorConfig(
        api_url="https://abc123.execute-api.eu-west-1.amazonaws.com",
        region="eu-west-1",
        profile="project-profile",
        poll_interval_seconds=3,
        request_timeout_seconds=20,
    )


def test_load_supervisor_config_rejects_non_aws_api_url_from_project_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, repo = _isolated_config_paths(monkeypatch, tmp_path)
    _write_supervisor_config(
        repo / ".awf.toml",
        {"api_url": "https://supervisor.example"},
    )

    with pytest.raises(ValueError):
        load_supervisor_config()


def test_load_supervisor_config_environment_overrides_file_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, repo = _isolated_config_paths(monkeypatch, tmp_path)
    _write_supervisor_config(
        home / ".config" / "awf" / "config.toml",
        {
            "api_url": "https://abc123.execute-api.us-east-1.amazonaws.com",
            "region": "us-east-1",
            "profile": "user-profile",
        },
    )
    _write_supervisor_config(
        repo / ".awf.toml",
        {
            "api_url": "https://abc123.execute-api.eu-west-1.amazonaws.com",
            "region": "eu-west-1",
            "profile": "project-profile",
            "poll_interval_seconds": 5,
            "request_timeout_seconds": 25,
        },
    )
    monkeypatch.setenv(
        "AWF_SUPERVISOR_API_URL",
        "https://abc123.execute-api.ap-southeast-2.amazonaws.com",
    )
    monkeypatch.setenv("AWF_SUPERVISOR_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_PROFILE", "environment-profile")

    config = load_supervisor_config()

    assert config == SupervisorConfig(
        api_url="https://abc123.execute-api.ap-southeast-2.amazonaws.com",
        region="ap-southeast-2",
        profile="environment-profile",
        poll_interval_seconds=5,
        request_timeout_seconds=25,
    )


def test_supervisor_config_allows_an_empty_api_url() -> None:
    assert _config_with(api_url="").api_url == ""


def test_supervisor_config_normalizes_stage_path_and_trailing_api_url_slash() -> None:
    assert (
        _config_with(
            api_url="https://abc123.execute-api.ap-northeast-2.amazonaws.com/prod/"
        ).api_url
        == "https://abc123.execute-api.ap-northeast-2.amazonaws.com/prod"
    )


@pytest.mark.parametrize(
    "api_url",
    [
        "http://abc123.execute-api.ap-northeast-2.amazonaws.com",
        "https://user:secret@abc123.execute-api.ap-northeast-2.amazonaws.com",
        "https://abc123.execute-api.ap-northeast-2.amazonaws.com?token=secret",
        "https://abc123.execute-api.ap-northeast-2.amazonaws.com#fragment",
        "https://api.example",
        "https://supervisor.example",
        "https://abc123.execute-api.ap-northeast-2.amazonaws.com:8443",
    ],
)
def test_supervisor_config_rejects_unsafe_or_non_aws_api_urls(api_url: str) -> None:
    with pytest.raises(ValueError):
        _config_with(api_url=api_url)


def test_supervisor_config_rejects_execute_api_url_for_a_different_region() -> None:
    with pytest.raises(ValueError):
        _config_with(
            api_url="https://abc123.execute-api.us-east-1.amazonaws.com",
            region="ap-northeast-2",
        )


@pytest.mark.parametrize(
    ("api_url", "region"),
    [
        (
            "https://abc123.execute-api.cn-north-1.amazonaws.com.cn",
            "cn-north-1",
        ),
        (
            "https://abc123.execute-api.us-gov-west-1.amazonaws.com",
            "us-gov-west-1",
        ),
    ],
)
def test_supervisor_config_rejects_execute_api_urls_from_unsupported_partitions(
    api_url: str, region: str
) -> None:
    with pytest.raises(ValueError):
        _config_with(api_url=api_url, region=region)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_interval_seconds", 0),
        ("poll_interval_seconds", -1),
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", -1),
        ("poll_interval_seconds", True),
        ("request_timeout_seconds", False),
        ("poll_interval_seconds", 1.5),
        ("request_timeout_seconds", 1.5),
        ("poll_interval_seconds", "2"),
        ("request_timeout_seconds", "30"),
    ],
)
def test_supervisor_config_rejects_invalid_numeric_settings(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        _config_with(**{field: value})


def test_load_supervisor_config_rejects_unknown_supervisor_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, repo = _isolated_config_paths(monkeypatch, tmp_path)
    _write_supervisor_config(
        repo / ".awf.toml",
        {
            "api_url": "https://abc123.execute-api.ap-northeast-2.amazonaws.com",
            "unsupported": True,
        },
    )

    with pytest.raises(ValueError):
        load_supervisor_config()


def test_supervisor_config_is_frozen() -> None:
    config = _config_with()

    with pytest.raises(FrozenInstanceError):
        config.region = "us-east-1"
