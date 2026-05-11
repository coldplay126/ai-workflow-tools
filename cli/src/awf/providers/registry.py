from __future__ import annotations

from typing import Callable

from awf.core.config import AwfConfig
from awf.core.permissions import build_permission_ruleset
from awf.providers.base import Provider, ProviderCapability
from awf.providers.claude_code import ClaudeCodeProvider
from awf.providers.claude_sdk import ClaudeSdkProvider
from awf.providers.codex import CodexProvider
from awf.providers.fixture import FixtureProvider
from awf.providers.gemini import GeminiProvider
from awf.providers.openai import OpenAiProvider

ProviderFactory = Callable[[AwfConfig], Provider]


class UnknownProviderError(KeyError):
    pass


class ProviderRegistry:
    def __init__(self, config: AwfConfig) -> None:
        self._config = config
        self._permission_ruleset = build_permission_ruleset(config.raw)
        self._builtin: dict[str, ProviderFactory] = {
            "claude-code": self._create_claude_code,
            "claude:sonnet": self._create_claude_sonnet,
            "claude-sdk": self._create_claude_sdk,
            "codex": self._create_codex,
            "fixture": self._create_fixture,
            "gemini": self._create_gemini,
            "openai": self._create_openai,
        }
        self._custom: dict[str, ProviderFactory] = {}

    def get(self, name: str) -> Provider:
        alias = self._alias(name)
        if alias in self._custom:
            return self._custom[alias](self._config)
        if alias in self._builtin:
            return self._builtin[alias](self._config)
        if name in self._custom:
            return self._custom[name](self._config)
        if name in self._builtin:
            return self._builtin[name](self._config)
        raise UnknownProviderError(name)

    def register(self, name: str, factory: ProviderFactory) -> None:
        self._custom[name] = factory

    def supports(self, name: str) -> bool:
        alias = self._alias(name)
        return alias in self._builtin or alias in self._custom or name in self._builtin or name in self._custom

    def capabilities(self, name: str) -> set[ProviderCapability]:
        provider = self.get(name)
        return set(getattr(provider, "capabilities", {ProviderCapability.COMPLETE}))

    def supports_capability(self, name: str, capability: ProviderCapability) -> bool:
        return capability in self.capabilities(name)

    def _alias(self, name: str) -> str:
        config_aliases = self._config.raw.get("provider", {}).get("aliases", {})
        if isinstance(config_aliases, dict) and name in config_aliases:
            return str(config_aliases[name])
        return name

    def _create_claude_code(self, config: AwfConfig) -> Provider:
        settings = config.provider_settings("claude-code")
        command = settings.get("command")
        flags = settings.get("flags")
        effort = settings.get("effort")
        json_schema = settings.get("json_schema")
        return ClaudeCodeProvider(
            command=str(command) if command else None,
            flags=list(flags) if isinstance(flags, list) else None,
            effort=str(effort) if effort else None,
            json_schema=str(json_schema) if json_schema else None,
        )

    def _create_claude_sonnet(self, config: AwfConfig) -> Provider:
        settings = config.provider_settings("claude-code")
        command = settings.get("command")
        flags = ["--print", "--model", "sonnet", "--permission-mode", "default"]
        effort = settings.get("effort")
        json_schema = settings.get("json_schema")
        return ClaudeCodeProvider(
            command=str(command) if command else None,
            flags=flags,
            verbose=False,
            effort=str(effort) if effort else None,
            json_schema=str(json_schema) if json_schema else None,
        )

    def _create_codex(self, config: AwfConfig) -> Provider:
        settings = config.provider_settings("codex")
        command = settings.get("command")
        flags = settings.get("flags")
        reasoning_effort = settings.get("reasoning_effort")
        output_schema_path = settings.get("output_schema_path")
        return CodexProvider(
            command=str(command) if command else None,
            flags=list(flags) if isinstance(flags, list) else None,
            reasoning_effort=str(reasoning_effort) if reasoning_effort else None,
            output_schema_path=str(output_schema_path) if output_schema_path else None,
        )

    def _create_claude_sdk(self, config: AwfConfig) -> Provider:
        settings = config.provider_settings("claude-sdk")
        api_key_env = settings.get("api_key_env")
        model = settings.get("model")
        max_tokens = settings.get("max_tokens")
        return ClaudeSdkProvider(
            api_key_env=str(api_key_env) if api_key_env else None,
            model=str(model) if model else None,
            max_tokens=int(max_tokens) if max_tokens else None,
            permission_ruleset=self._permission_ruleset,
        )

    def _create_fixture(self, config: AwfConfig) -> Provider:
        settings = config.provider_settings("fixture")
        result_file = settings.get("result_file", "")
        return FixtureProvider(str(result_file))

    def _create_gemini(self, config: AwfConfig) -> Provider:
        settings = config.provider_settings("gemini")
        command = settings.get("command")
        flags = settings.get("flags")
        model = settings.get("model")
        return GeminiProvider(
            command=str(command) if command else None,
            flags=list(flags) if isinstance(flags, list) else None,
            model=str(model) if model is not None else None,
        )

    def _create_openai(self, config: AwfConfig) -> Provider:
        settings = config.provider_settings("openai")
        api_key_env = settings.get("api_key_env")
        model = settings.get("model")
        max_output_tokens = settings.get("max_output_tokens")
        return OpenAiProvider(
            api_key_env=str(api_key_env) if api_key_env else None,
            model=str(model) if model else None,
            max_output_tokens=int(max_output_tokens) if max_output_tokens else None,
            permission_ruleset=self._permission_ruleset,
        )
