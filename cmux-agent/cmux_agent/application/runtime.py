"""Runtime helpers for dynamic cmux agent sessions."""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cmux_agent.application.prompting import PromptBuilder
from cmux_agent.domain.events import agent_registered
from cmux_agent.domain.models import Agent, AgentRole
from cmux_agent.infrastructure.cmux import CmuxAdapter
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


PROVIDER_COMMANDS = {
    "codex": "codex",
}

DEFAULT_PROVIDER_FALLBACKS = {
    "gemini": ("claude", "codex"),
    "claude": ("codex", "gemini"),
    "codex": ("claude", "gemini"),
}


def normalize_agent_entry(entry: str | dict | None) -> dict:
    """Normalize a provider config entry into {provider, flags} shape."""
    if entry is None:
        return {}
    if isinstance(entry, str):
        return {"provider": entry}
    return dict(entry)


@dataclass(frozen=True)
class ProviderSelection:
    provider: str
    flags: str
    requested_provider: str
    used_fallback: bool = False


def _provider_binary(provider: str) -> str:
    command = PROVIDER_COMMANDS.get(provider, provider)
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    return parts[0] if parts else command


def _provider_available(
    provider: str,
    *,
    command_exists: Callable[[str], str | None] | None = None,
) -> bool:
    exists = command_exists or shutil.which
    return bool(exists(_provider_binary(provider)))


def _fallback_entries(entry: dict) -> list[dict]:
    raw = entry.get("fallbacks", entry.get("fallback", []))
    if not raw:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [normalize_agent_entry(item) for item in raw]


def resolve_provider_selection(
    entry: str | dict | None,
    *,
    default_provider: str = "claude",
    command_exists: Callable[[str], str | None] | None = None,
) -> ProviderSelection:
    normalized = normalize_agent_entry(entry)
    requested = str(normalized.get("provider", "") or default_provider)
    flags = str(normalized.get("flags", "") or "")
    if _provider_available(requested, command_exists=command_exists):
        return ProviderSelection(
            provider=requested,
            flags=flags,
            requested_provider=requested,
        )

    candidates = _fallback_entries(normalized)
    candidates.extend({"provider": provider} for provider in DEFAULT_PROVIDER_FALLBACKS.get(requested, ()))

    seen = {requested}
    for candidate in candidates:
        provider = str(candidate.get("provider", "") or "")
        if not provider or provider in seen:
            continue
        seen.add(provider)
        if _provider_available(provider, command_exists=command_exists):
            return ProviderSelection(
                provider=provider,
                flags=str(candidate.get("flags", "") or ""),
                requested_provider=requested,
                used_fallback=True,
            )

    return ProviderSelection(
        provider=requested,
        flags=flags,
        requested_provider=requested,
    )


def provider_command(provider: str, flags: str = "") -> str:
    command = PROVIDER_COMMANDS.get(provider, provider)
    flags = flags.strip()
    if flags:
        command = f"{command} {flags}"
    return command


def parse_surface_ref(output: str) -> str | None:
    """Extract a cmux surface ref from `new-surface` output."""
    for token in output.split():
        if token.startswith("surface:"):
            return token
    return None


@dataclass(frozen=True)
class SpawnResult:
    ok: bool
    name: str
    surface_id: str | None = None
    provider: str | None = None
    error: str | None = None


class AgentRuntime:
    """Creates and registers dynamic worker sessions for an active run."""

    def __init__(
        self,
        *,
        store: StateStore,
        event_log: EventLog,
        fs: AgentFileSystem,
        cmux: CmuxAdapter,
        prompt_builder: PromptBuilder,
        run_id: str,
        workspace_id: str | None,
        template_dir: Path | None = None,
        provider_config: dict | None = None,
    ) -> None:
        self._store = store
        self._event_log = event_log
        self._fs = fs
        self._cmux = cmux
        self._prompt = prompt_builder
        self._run_id = run_id
        self._workspace_id = workspace_id
        self._template_dir = template_dir
        self._provider_config = provider_config or {}

    def spawn_worker(
        self,
        *,
        name: str | None = None,
        role: str | None = None,
        template: str | None = None,
        provider: str | None = None,
        flags: str | None = None,
    ) -> SpawnResult:
        purpose = template or role
        worker_name = self._resolve_worker_name(name, purpose=purpose)
        if self._store.get_agent_by_name(self._run_id, worker_name):
            return SpawnResult(ok=False, name=worker_name, error="agent already exists")

        entry = self._provider_entry(worker_name, purpose=purpose)
        if provider is not None:
            selected = resolve_provider_selection(
                {"provider": provider, "flags": flags or ""},
            )
        else:
            selected = resolve_provider_selection(entry)

        created = self._cmux.new_surface(workspace_id=self._workspace_id)
        if not created.ok:
            return SpawnResult(ok=False, name=worker_name, provider=selected.provider, error=created.stderr or "cmux new-surface failed")

        surface_id = parse_surface_ref(created.stdout)
        if not surface_id:
            return SpawnResult(ok=False, name=worker_name, provider=selected.provider, error="cmux surface ref missing")

        agent = Agent(
            run_id=self._run_id,
            role=AgentRole.WORKER,
            name=worker_name,
            surface_id=surface_id,
        )
        self._store.save_agent(agent)
        self._fs.create_inbox(worker_name)
        self._event_log.append(agent_registered(self._run_id, worker_name, AgentRole.WORKER.value))

        self._cmux.rename_tab(worker_name, surface_id=surface_id, workspace_id=self._workspace_id)
        self._prompt.write_protocol_files(
            self._fs.base,
            self._store.get_agents(self._run_id),
            template_dir=self._template_dir,
        )

        command = provider_command(selected.provider, selected.flags)
        self._cmux.send_text(f"{command}\n", surface_id=surface_id, workspace_id=self._workspace_id)
        self._cmux.send_text(
            self._prompt.build_startup_prompt(agent),
            surface_id=surface_id,
            workspace_id=self._workspace_id,
        )
        self._cmux.send_key("enter", surface_id=surface_id, workspace_id=self._workspace_id)
        if selected.used_fallback:
            self._cmux.log(
                f"provider fallback: {worker_name} {selected.requested_provider} -> {selected.provider}",
                level="warning",
                source="cmux-agent",
                workspace_id=self._workspace_id,
            )
        self._cmux.notify(title="cmux-agent", body=f"Worker spawned: {worker_name}")
        self._cmux.log(
            f"worker spawned: {worker_name} ({selected.provider})",
            level="success",
            source="cmux-agent",
            workspace_id=self._workspace_id,
        )
        return SpawnResult(ok=True, name=worker_name, surface_id=surface_id, provider=selected.provider)

    def _resolve_worker_name(self, requested: str | None, *, purpose: str | None = None) -> str:
        if requested:
            return _sanitize_agent_name(requested)

        used = {
            agent.name
            for agent in self._store.get_agents(self._run_id)
            if agent.role == AgentRole.WORKER
        }
        base_name = _worker_base_name(purpose)
        if base_name:
            return _next_worker_name(base_name, used, numbered_first=False)
        return _next_worker_name("worker-auto", used, numbered_first=True)

    def _provider_entry(self, worker_name: str, *, purpose: str | None = None) -> dict:
        keys = [worker_name]
        base_name = _worker_base_name(purpose)
        if base_name and base_name not in keys:
            keys.append(base_name)
        suffix_base = _strip_numeric_suffix(worker_name)
        if suffix_base != worker_name and suffix_base not in keys:
            keys.append(suffix_base)

        for key in keys:
            if key in self._provider_config:
                return normalize_agent_entry(self._provider_config.get(key))
        return {}


def _sanitize_agent_name(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-")
    return normalized or "worker"


def _sanitize_worker_purpose(purpose: str | None) -> str:
    if not purpose:
        return ""
    return _sanitize_agent_name(str(purpose)).lower()


def _worker_base_name(purpose: str | None) -> str | None:
    normalized = _sanitize_worker_purpose(purpose)
    if not normalized or normalized == "worker":
        return None
    if normalized.startswith("worker-"):
        return normalized
    return f"worker-{normalized}"


def _next_worker_name(base_name: str, used: set[str], *, numbered_first: bool) -> str:
    if not numbered_first and base_name not in used:
        return base_name

    idx = 1 if numbered_first else 2
    while f"{base_name}-{idx}" in used:
        idx += 1
    return f"{base_name}-{idx}"


def _strip_numeric_suffix(name: str) -> str:
    return re.sub(r"-\d+$", "", name)
