from __future__ import annotations

import os
import json
import selectors
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Iterator, Optional

from awf.core.events import EventType, ExecutionEvent, EventStream
from awf.core.task import TaskDefinition, TaskType
from awf.providers.base import GatewayProvider, ProviderCapability, ProviderResult
from awf.providers.subprocess_provider import SpawnSpec, SubprocessProvider


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_stream_json_event(
    payload: dict,
    *,
    run_id: str,
    task_id: str,
    task_type: str,
    source: str,
    sequence: int,
) -> list[ExecutionEvent]:
    events: list[ExecutionEvent] = []
    payload_type = str(payload.get("type", ""))
    if payload_type == "stream_event":
        event = payload.get("event", {})
        event_type = str(event.get("type", ""))
        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            text = str(delta.get("text", "") or "")
            partial_json = str(delta.get("partial_json", "") or "")
            output_text = text or partial_json
            if output_text:
                events.append(
                    ExecutionEvent(
                        type=EventType.PROVIDER_OUTPUT,
                        timestamp=_now_iso(),
                        run_id=run_id,
                        task_id=task_id,
                        source=source,
                        sequence=sequence,
                        data={"text": output_text, "stream": True},
                    )
                )
        elif event_type == "content_block_start":
            content_block = event.get("content_block", {})
            if content_block.get("type") == "tool_use":
                tool_name = str(content_block.get("name", ""))
                events.append(
                    ExecutionEvent(
                        type=EventType.PROVIDER_TOOL_CALL,
                        timestamp=_now_iso(),
                        run_id=run_id,
                        task_id=task_id,
                        source=source,
                        sequence=sequence,
                        data={"tool": tool_name},
                    )
                )
        elif event_type in {"message_start", "content_block_stop", "message_stop"}:
            pass
        else:
            events.append(
                ExecutionEvent(
                    type=EventType.PROGRESS_UPDATE,
                    timestamp=_now_iso(),
                    run_id=run_id,
                    task_id=task_id,
                    source=source,
                    sequence=sequence,
                    data={"message": f"claude_stream:{event_type}", "detail": event},
                )
            )
    elif payload_type == "assistant":
        pass
    elif payload_type == "result":
        is_error = bool(payload.get("is_error", False))
        events.append(
            ExecutionEvent(
                type=EventType.TASK_FAILED if is_error else EventType.TASK_COMPLETED,
                timestamp=_now_iso(),
                run_id=run_id,
                task_id=task_id,
                source=source,
                sequence=sequence,
                data={
                    "task_type": task_type,
                    "duration_sec": float(payload.get("duration_ms", 0) or 0) / 1000.0,
                    "returncode": 1 if is_error else 0,
                    "result": str(payload.get("result", "") or ""),
                    "stop_reason": payload.get("stop_reason"),
                },
            )
        )
    elif payload_type == "system":
        subtype = str(payload.get("subtype", "system"))
        events.append(
            ExecutionEvent(
                type=EventType.PROGRESS_UPDATE,
                timestamp=_now_iso(),
                run_id=run_id,
                task_id=task_id,
                source=source,
                sequence=sequence,
                data={"message": f"system:{subtype}", "detail": payload},
            )
        )
    elif payload_type == "rate_limit_event":
        events.append(
            ExecutionEvent(
                type=EventType.PROGRESS_UPDATE,
                timestamp=_now_iso(),
                run_id=run_id,
                task_id=task_id,
                source=source,
                sequence=sequence,
                data={"message": "rate_limit_event", "detail": payload.get("rate_limit_info")},
            )
        )
    return events


class ClaudeCodeEventStream(EventStream):
    def __init__(self, process: subprocess.Popen[str], *, task: TaskDefinition, source: str) -> None:
        self._process = process
        self._task = task
        self._source = source
        self._run_id = str(uuid.uuid4())
        self._sequence = 0
        self._cancelled = False

    def __iter__(self) -> Iterator[ExecutionEvent]:
        yield self._emit(
            EventType.TASK_STARTED,
            {
                "task_type": self._task.type.value,
                "provider": self._source,
                "mode": "native",
                "params": dict(self._task.params),
            },
        )
        assert self._process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        started_at = time.monotonic()
        heartbeat_sec = 3
        next_heartbeat = heartbeat_sec
        while selector.get_map():
            events = selector.select(timeout=1.0)
            if not events:
                elapsed = time.monotonic() - started_at
                if elapsed >= next_heartbeat:
                    yield self._emit(
                        EventType.HEARTBEAT,
                        {"elapsed_sec": elapsed, "label": f"{self._source} {self._task.type.value}"},
                    )
                    next_heartbeat += heartbeat_sec
                if self._process.poll() is not None:
                    break
                continue
            for key, _ in events:
                stream = key.fileobj
                raw_line = stream.readline()
                if raw_line == "":
                    selector.unregister(stream)
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    yield self._emit(EventType.PROVIDER_OUTPUT, {"text": line, "stream": True})
                    continue
                parsed = _parse_stream_json_event(
                    payload,
                    run_id=self._run_id,
                    task_id=self._task.task_id,
                    task_type=self._task.type.value,
                    source=self._source,
                    sequence=self._sequence,
                )
                for event in parsed:
                    event.sequence = self._sequence
                    self._sequence += 1
                    yield event
        returncode = self._process.wait(timeout=max(1, 1))
        if returncode not in (0,) and not self._cancelled:
            yield self._emit(
                EventType.TASK_FAILED,
                {
                    "task_type": self._task.type.value,
                    "error": f"claude-code exited with returncode {returncode}",
                    "returncode": returncode,
                },
            )

    def cancel(self) -> None:
        self._cancelled = True
        if self._process.poll() is None:
            self._process.kill()

    def _emit(self, event_type: EventType, data: dict) -> ExecutionEvent:
        event = ExecutionEvent(
            type=event_type,
            timestamp=_now_iso(),
            run_id=self._run_id,
            task_id=self._task.task_id,
            source=self._source,
            sequence=self._sequence,
            data=data,
        )
        self._sequence += 1
        return event


class ClaudeCodeProvider(SubprocessProvider, GatewayProvider):
    name = "claude-code"
    capabilities = {
        ProviderCapability.COMPLETE,
        ProviderCapability.ANALYZE_NATIVE,
        ProviderCapability.WF_NATIVE,
        ProviderCapability.TOOL_LOOP,
        ProviderCapability.EVENT_STREAM,
        ProviderCapability.ADD_DIR,
    }

    def __init__(
        self,
        command: Optional[str] = None,
        flags: Optional[list[str]] = None,
        verbose: bool = True,
        effort: Optional[str] = None,
        json_schema: Optional[str] = None,
    ) -> None:
        resolved_command = command or os.environ.get("AWF_CLAUDE_COMMAND", "claude")
        if flags is not None:
            resolved_flags = flags
        else:
            env_flags = os.environ.get("AWF_CLAUDE_FLAGS")
            resolved_flags = shlex.split(env_flags) if env_flags else ["--print", "--permission-mode", "default"]
        super().__init__(command=resolved_command, flags=resolved_flags)
        self.timeout_sec = int(os.environ.get("AWF_CLAUDE_TIMEOUT_SEC", "900"))
        self.verbose = verbose
        self.effort = effort
        self.json_schema = json_schema or os.environ.get("AWF_CLAUDE_JSON_SCHEMA")

    def set_permission_mode(self, mode: str) -> None:
        """Set or replace Claude Code permission mode."""
        for index, flag in enumerate(self.flags):
            if flag == "--permission-mode" and index + 1 < len(self.flags):
                self.flags[index + 1] = mode
                return
            if flag.startswith("--permission-mode="):
                self.flags[index] = f"--permission-mode={mode}"
                return
        self.flags.extend(["--permission-mode", mode])

    def set_model(self, model: str) -> None:
        """Set or replace Claude Code model. Accepts CLI aliases (sonnet, opus, haiku) or full ids."""
        for index, flag in enumerate(self.flags):
            if flag == "--model" and index + 1 < len(self.flags):
                self.flags[index + 1] = model
                return
            if flag.startswith("--model="):
                self.flags[index] = f"--model={model}"
                return
        self.flags.extend(["--model", model])

    def build_spawn_spec(
        self,
        prompt: str,
        *,
        add_dirs: list[str] | None = None,
        stream_json: bool = False,
    ) -> SpawnSpec:
        """Build the shared Claude Code invocation used by complete and streaming runs."""
        del stream_json
        cmd = [self.command, *self.flags]
        if self.effort:
            cmd.extend(["--effort", self.effort])
        for directory in add_dirs or []:
            cmd.extend(["--add-dir", directory])
        if self.json_schema:
            cmd.extend(["--json-schema", self.json_schema])
        cmd.append(prompt)
        return SpawnSpec(argv=cmd)

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        effective_timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        spawn_spec = self.build_spawn_spec(prompt, add_dirs=add_dirs)
        try:
            started_at = time.monotonic()
            process = subprocess.Popen(
                spawn_spec.argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                bufsize=1,
            )
            if self.verbose:
                print(
                    f"provider_process_started: claude-code pid={process.pid} add_dirs={len(add_dirs or [])} prompt_chars={len(prompt)}",
                    file=sys.stderr,
                )
            selector = selectors.DefaultSelector()
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            if process.stdout is not None:
                selector.register(process.stdout, selectors.EVENT_READ, data="stdout")
            if process.stderr is not None:
                selector.register(process.stderr, selectors.EVENT_READ, data="stderr")
            try:
                while selector.get_map():
                    if process.poll() is not None and not selector.get_map():
                        break
                    events = selector.select(timeout=0.5)
                    if not events and process.poll() is not None:
                        break
                    for key, _ in events:
                        stream = key.fileobj
                        chunk = stream.readline()
                        if chunk == "":
                            selector.unregister(stream)
                            continue
                        if key.data == "stdout":
                            stdout_chunks.append(chunk)
                        else:
                            stderr_chunks.append(chunk)
                            if self.verbose:
                                print(f"provider_stderr: claude-code {chunk.rstrip()}", file=sys.stderr)
                returncode = process.wait(timeout=max(1, effective_timeout))
            except subprocess.TimeoutExpired:
                process.kill()
                return ProviderResult(
                    returncode=124,
                    stdout="".join(stdout_chunks),
                    stderr=f"provider_timeout: claude-code timed out after {effective_timeout}s\n" + "".join(stderr_chunks),
                )
            wall_time = time.monotonic() - started_at
            if self.verbose:
                print(
                    f"provider_subprocess_wall: claude-code {wall_time:.1f}s pid={process.pid}",
                    file=sys.stderr,
                )
            return ProviderResult(
                returncode=returncode,
                stdout="".join(stdout_chunks),
                stderr="".join(stderr_chunks),
            )
        except FileNotFoundError:
            return ProviderResult(
                returncode=127,
                stdout="",
                stderr=f"claude-code command not found: {self.command}",
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                returncode=124,
                stdout="",
                stderr=f"provider_timeout: claude-code timed out after {effective_timeout}s",
            )

    def execute(self, task: TaskDefinition) -> EventStream:
        if task.type not in {TaskType.ANALYZE, TaskType.WF_PHASE}:
            raise NotImplementedError(f"claude-code execute() currently supports analyze/wf_phase only: {task.type.value}")
        prompt = str(task.params.get("prompt", "") or "")
        if not prompt:
            raise ValueError("claude-code native execute requires task.params['prompt']")
        cwd = task.context.cwd
        flags = [flag for flag in self.flags if flag not in {"--print", "-p"}]
        if "--verbose" not in flags:
            flags.append("--verbose")
        if "--output-format" not in flags:
            flags.extend(["--output-format", "stream-json"])
        if "--include-partial-messages" not in flags:
            flags.append("--include-partial-messages")
        if self.effort and "--effort" not in flags:
            flags.extend(["--effort", self.effort])
        add_dirs = task.params.get("add_dirs") or []
        if isinstance(add_dirs, list):
            for directory in add_dirs:
                flags.extend(["--add-dir", str(directory)])
        if self.json_schema and "--json-schema" not in flags:
            flags.extend(["--json-schema", self.json_schema])
        cmd = [self.command, *flags, prompt]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            bufsize=1,
        )
        return ClaudeCodeEventStream(process, task=task, source=self.name)
