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
_AUTH_RE = re.compile(
    r"credential|authentication|authorize|not logged in|login required|not authenticated|api[ _-]?key",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(
    r"\bunsupported\s+model\b|(?<!tool )(?<!feature )(?<!parameter )"
    r"\bmodel(?:\s+\S+)?\s+is\s+not\s+supported\b",
    re.IGNORECASE,
)


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
    if _EXPIRED_RE.search(text):
        return "host_subscription_expired"
    if _MODEL_RE.search(text):
        return "host_model_unsupported"
    if _AUTH_RE.search(text):
        return "host_auth_unavailable"
    return "host_provider_exit"
