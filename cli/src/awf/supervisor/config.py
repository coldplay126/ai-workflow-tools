from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from awf.core.config import load_awf_config


_SUPERVISOR_FIELDS = frozenset(
    {
        "api_url",
        "region",
        "profile",
        "poll_interval_seconds",
        "request_timeout_seconds",
    }
)

_COMMERCIAL_AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[1-9][0-9]*$")
_UNSUPPORTED_AWS_REGION_PREFIXES = ("cn-", "us-gov-", "us-iso", "eu-isoe-")



@dataclass(frozen=True)
class SupervisorConfig:
    api_url: str
    region: str
    profile: str
    poll_interval_seconds: int
    request_timeout_seconds: int

    def __post_init__(self) -> None:
        region = _validate_commercial_aws_region(self.region)
        object.__setattr__(self, "api_url", _normalize_api_url(self.api_url, region))
        _require_string("profile", self.profile)
        _require_positive_int("poll_interval_seconds", self.poll_interval_seconds)
        _require_positive_int("request_timeout_seconds", self.request_timeout_seconds)


def load_supervisor_config(explicit_root: Optional[str] = None) -> SupervisorConfig:
    raw = load_awf_config(explicit_root).raw
    supervisor = raw.get("supervisor", {})
    if not isinstance(supervisor, Mapping):
        raise ValueError("supervisor configuration must be a table")

    unknown_keys = set(supervisor).difference(_SUPERVISOR_FIELDS)
    if unknown_keys:
        unknown = ", ".join(sorted(repr(key) for key in unknown_keys))
        raise ValueError("unknown supervisor configuration key(s): {}".format(unknown))

    values = dict(supervisor)
    for environment_key, config_key in (
        ("AWF_SUPERVISOR_API_URL", "api_url"),
        ("AWF_SUPERVISOR_REGION", "region"),
        ("AWS_PROFILE", "profile"),
    ):
        if environment_key in os.environ:
            values[config_key] = os.environ[environment_key]

    try:
        return SupervisorConfig(**values)
    except TypeError as exc:
        raise ValueError("supervisor configuration has invalid fields") from exc


def _normalize_api_url(value: str, region: str) -> str:
    _require_string("api_url", value)
    if not value:
        return value

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("api_url must be a valid HTTPS URL") from exc

    if parsed.scheme != "https" or not parsed.netloc or parsed.hostname is None:
        raise ValueError("api_url must be an HTTPS URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api_url must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("api_url must not include a query or fragment")
    if port not in (None, 443):
        raise ValueError("api_url must use the default HTTPS port")

    host = parsed.netloc.rsplit(":", 1)[0] if port is not None else parsed.netloc
    suffix = ".execute-api.{}.amazonaws.com".format(region)
    api_id = host[: -len(suffix)] if host.endswith(suffix) else ""
    if not api_id or not api_id.isascii() or not api_id.isalnum() or api_id != api_id.lower():
        raise ValueError(
            "api_url must be an execute-api URL for the configured commercial AWS region"
        )

    return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))


def _validate_commercial_aws_region(value: Any) -> str:
    _require_non_empty_string("region", value)
    if (
        not _COMMERCIAL_AWS_REGION_PATTERN.fullmatch(value)
        or value.startswith(_UNSUPPORTED_AWS_REGION_PREFIXES)
    ):
        raise ValueError("region must be a supported commercial AWS region")
    return value


def _require_string(field: str, value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("{} must be a string".format(field))


def _require_non_empty_string(field: str, value: Any) -> None:
    _require_string(field, value)
    if not value:
        raise ValueError("{} must not be empty".format(field))


def _require_positive_int(field: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("{} must be a positive integer".format(field))
