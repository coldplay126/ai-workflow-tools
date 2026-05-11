"""Runtime helpers for dynamic cmux agent sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

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


def normalize_agent_entry(entry: str | dict | None) -> dict:
    """Normalize a provider config entry into {provider, flags} shape."""
    if entry is None:
        return {}
    if isinstance(entry, str):
        return {"provider": entry}
    return dict(entry)


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
        selected_provider = provider or str(entry.get("provider", "") or "claude")
        selected_flags = flags if flags is not None else str(entry.get("flags", "") or "")

        created = self._cmux.new_surface(workspace_id=self._workspace_id)
        if not created.ok:
            return SpawnResult(ok=False, name=worker_name, provider=selected_provider, error=created.stderr or "cmux new-surface failed")

        surface_id = parse_surface_ref(created.stdout)
        if not surface_id:
            return SpawnResult(ok=False, name=worker_name, provider=selected_provider, error="cmux surface ref missing")

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

        command = provider_command(selected_provider, selected_flags)
        self._cmux.send_text(f"{command}\n", surface_id=surface_id, workspace_id=self._workspace_id)
        self._cmux.send_text(
            self._prompt.build_startup_prompt(agent),
            surface_id=surface_id,
            workspace_id=self._workspace_id,
        )
        self._cmux.send_key("enter", surface_id=surface_id, workspace_id=self._workspace_id)
        self._cmux.notify(title="cmux-agent", body=f"Worker spawned: {worker_name}")
        self._cmux.log(
            f"worker spawned: {worker_name} ({selected_provider})",
            level="success",
            source="cmux-agent",
            workspace_id=self._workspace_id,
        )
        return SpawnResult(ok=True, name=worker_name, surface_id=surface_id, provider=selected_provider)

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
