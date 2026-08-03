from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

SUPPORTED_RUNTIMES = frozenset({"claude", "agent-skills", "omp"})
PINNED_SUBSCRIPTION_MODELS = MappingProxyType(
    {
        "claude": "sonnet",
        "agent-skills": "gpt-5.4",
        "omp": "openai-codex/gpt-5.6-sol",
    }
)
API_KEY_ENV_KEYS = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})
CONFIG_ENV_KEYS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR")

_EXPIRED_RE = re.compile(r"refresh token expired|subscription[^\n]*expired", re.IGNORECASE)
_OAUTH_SESSION_REFRESH_FAILED_RE = re.compile(
    r"\boauth[ \t]+session(?:[ \t]+has)?[ \t]+expired\b"
    r"[^\r\n]{0,160}?\b(?:"
    r"could[ \t]+not[ \t]+be[ \t]+refreshed"
    r"|failed[ \t]+to[ \t]+refresh"
    r"|refresh[ \t]+failed"
    r")\b",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"credential|authentication|authorize|not logged in|login required|not authenticated|api[ _-]?key",
    re.IGNORECASE,
)
_QUOTED_MODEL_IDENTIFIER_RE = r"""(?:'[^'\r\n]+'|"[^"\r\n]+")"""
_MODEL_IDENTIFIER_RE = rf"(?:{_QUOTED_MODEL_IDENTIFIER_RE}|\S*[0-9._/-]\S*)"
_MODEL_IDENTIFIER_PATTERN = re.compile(_MODEL_IDENTIFIER_RE)
_PINNED_SIMPLE_WORD_SUBSCRIPTION_MODELS = frozenset(
    model.casefold()
    for model in PINNED_SUBSCRIPTION_MODELS.values()
    if not _MODEL_IDENTIFIER_PATTERN.fullmatch(model)
)
_UNSUPPORTED_MODEL_STATUS_RE = r"is[ \t]+(?:not[ \t]+supported|unsupported)\b"
_EXPLICIT_UNSUPPORTED_MODEL_RE = re.compile(
    rf"\bmodel[ \t]+(?P<identifier>{_QUOTED_MODEL_IDENTIFIER_RE}|\S+)[ \t]+{_UNSUPPORTED_MODEL_STATUS_RE}",
    re.IGNORECASE,
)
_NON_MODEL_PREFIX_RE = r"(?<!tool[ \t])(?<!feature[ \t])(?<!parameter[ \t])"
_UNSUPPORTED_MODEL_ASSERTIONS = (
    re.compile(r"\bunsupported[ \t]+model\b", re.IGNORECASE),
    re.compile(
        rf"{_NON_MODEL_PREFIX_RE}\bmodel[ \t]+{_UNSUPPORTED_MODEL_STATUS_RE}",
        re.IGNORECASE,
    ),
)


def _has_explicit_unsupported_model(text: str) -> bool:
    for match in _EXPLICIT_UNSUPPORTED_MODEL_RE.finditer(text):
        identifier = match["identifier"]
        if _MODEL_IDENTIFIER_PATTERN.fullmatch(identifier) or (
            identifier.casefold() in _PINNED_SIMPLE_WORD_SUBSCRIPTION_MODELS
        ):
            return True
    return False


def _resolve_auth_path(value: str | Path, original_home: Path) -> Path:
    path_value = os.fspath(value)
    if path_value == "~":
        path = original_home
    elif path_value.startswith("~/"):
        path = original_home / path_value[2:]
    else:
        path = Path(path_value)
    return path.resolve()


@dataclass(frozen=True)
class SubscriptionAuthContext:
    original_home: Path
    claude_config_dir: Path
    codex_home: Path
    omp_agent_dir: Path

    @classmethod
    def capture(
        cls, environment: Mapping[str, str] | None = None
    ) -> SubscriptionAuthContext:
        source = dict(os.environ if environment is None else environment)
        home_value = source.get("HOME")
        if not home_value:
            raise ValueError("subscription auth requires HOME")
        home = Path(home_value).expanduser().resolve()
        return cls(
            original_home=home,
            claude_config_dir=_resolve_auth_path(
                source.get("CLAUDE_CONFIG_DIR", home / ".claude"),
                home,
            ),
            codex_home=_resolve_auth_path(source.get("CODEX_HOME", home / ".codex"), home),
            omp_agent_dir=_resolve_auth_path(
                source.get("PI_CODING_AGENT_DIR", home / ".omp" / "agent"),
                home,
            ),
        )


def require_subscription_model(runtime: str, model: str) -> None:
    expected = PINNED_SUBSCRIPTION_MODELS.get(runtime)
    if expected is None:
        raise ValueError(f"unsupported runtime: {runtime}")
    if model != expected:
        raise ValueError(f"subscription model mismatch for {runtime}: expected {expected}")


def build_subscription_environment(
    runtime: str,
    auth: SubscriptionAuthContext,
    temporary_home: Path,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"unsupported runtime: {runtime}")

    environment = dict(os.environ if base_environment is None else base_environment)
    for key in (*CONFIG_ENV_KEYS, *API_KEY_ENV_KEYS):
        environment.pop(key, None)

    if runtime == "claude":
        environment["HOME"] = str(auth.original_home)
        environment["CLAUDE_CONFIG_DIR"] = str(auth.claude_config_dir)
    else:
        environment["HOME"] = str(temporary_home)
        if runtime == "agent-skills":
            environment["CODEX_HOME"] = str(auth.codex_home)
        else:
            environment["PI_CODING_AGENT_DIR"] = str(auth.omp_agent_dir)
    return environment


def normalize_host_diagnostic(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    error_kind: str | None = None,
) -> str:
    if error_kind == "timeout" or returncode == 124:
        return "host_timeout"
    if returncode == 0:
        return ""
    text = f"{stdout}\n{stderr}"
    if _EXPIRED_RE.search(text) or _OAUTH_SESSION_REFRESH_FAILED_RE.search(text):
        return "host_subscription_expired"
    if any(
        pattern.search(text) for pattern in _UNSUPPORTED_MODEL_ASSERTIONS
    ) or _has_explicit_unsupported_model(text):
        return "host_model_unsupported"
    if _AUTH_RE.search(text):
        return "host_auth_unavailable"
    return "host_provider_exit"
