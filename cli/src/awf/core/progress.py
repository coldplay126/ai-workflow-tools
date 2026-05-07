from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO

from awf.core.events import EventType, ExecutionEvent


@dataclass
class ProgressDisplay:
    stream: TextIO = sys.stderr
    _output_tokens: int = 0
    _tool_calls: int = 0
    _last_tool: str = ""
    _thinking_events: int = 0
    _last_progress_len: int = 0
    _task_completed: bool = False

    def handle(self, event: ExecutionEvent) -> None:
        if event.type == EventType.TASK_STARTED:
            if event.data.get("display", True) is False:
                return
            self._output_tokens = 0
            self._tool_calls = 0
            self._last_tool = ""
            self._thinking_events = 0
            self._task_completed = False
            task_type = str(event.data.get("task_type", "task"))
            provider = str(event.data.get("provider", event.source))
            mode = str(event.data.get("mode", "") or "").strip()
            label = f"{provider}" + (f" ({mode})" if mode else "")
            self._print(f"  ▶ {task_type} — {label}")

        elif event.type == EventType.TASK_COMPLETED:
            if event.data.get("display", True) is False:
                return
            self._task_completed = True
            if self._last_progress_len > 0 and self._is_tty():
                self.stream.write("\r" + " " * self._last_progress_len + "\r")
                self.stream.flush()
                self._last_progress_len = 0
            task_type = str(event.data.get("task_type", "task"))
            duration = event.data.get("duration_sec")
            returncode = int(event.data.get("returncode", 0) or 0)
            icon = "✓" if returncode == 0 else "✗"
            parts = []
            if duration is not None:
                parts.append(f"{float(duration):.1f}s")
            if self._output_tokens > 0:
                parts.append(f"↑ {self._output_tokens:,} tokens")
            if self._tool_calls > 0:
                parts.append(f"{self._tool_calls} tools")
            detail = f" ({', '.join(parts)})" if parts else ""
            self._print(f"  {icon} {task_type} 완료{detail}")

        elif event.type == EventType.TASK_FAILED:
            if event.data.get("display", True) is False:
                return
            self._task_completed = True
            task_type = str(event.data.get("task_type", "task"))
            error = str(event.data.get("error", "") or "").strip()
            msg = f"  ✗ {task_type} 실패"
            if error:
                msg += f" — {error[:80]}"
            self._print(msg)

        elif event.type == EventType.PROVIDER_OUTPUT:
            text = str(event.data.get("text", ""))
            self._output_tokens += max(1, len(text) // 4)

        elif event.type == EventType.PROVIDER_TOOL_CALL:
            self._tool_calls += 1
            tool_name = str(event.data.get("tool", event.data.get("name", "")))
            if tool_name:
                self._last_tool = tool_name

        elif event.type == EventType.HEARTBEAT:
            if self._task_completed:
                return
            label = str(event.data.get("label", "task"))
            elapsed = float(event.data.get("elapsed_sec", 0) or 0)
            parts = [f"{elapsed:.0f}s"]
            if self._output_tokens > 0:
                parts.append(f"↑ {self._output_tokens:,} tokens")
            if self._tool_calls > 0:
                tool_info = f"{self._tool_calls} tools"
                if self._last_tool:
                    tool_info += f" ({self._last_tool})"
                parts.append(tool_info)
            line = f"    ⟳ {label} · {' · '.join(parts)}"
            if self._is_tty():
                padding = max(0, self._last_progress_len - len(line))
                self.stream.write(f"\r{line}{' ' * padding}")
                self.stream.flush()
                self._last_progress_len = len(line)
            else:
                self._print(line)

        elif event.type == EventType.PROGRESS_UPDATE:
            message = str(event.data.get("message", ""))
            if "system:" in message:
                self._thinking_events += 1

        elif event.type == EventType.ESCAPE_TRIGGERED:
            reason = str(event.data.get("reason", "") or "").strip()
            summary = str(event.data.get("summary", "") or "").strip()
            msg = f"  ⚠ escape: {reason}"
            if summary:
                msg += f" — {summary[:80]}"
            self._print(msg)

        elif event.type == EventType.ORCHESTRATOR_DECIDED:
            decision = str(event.data.get("decision", "decision") or "decision")
            reason = str(event.data.get("reason", "") or "").strip()
            msg = f"  → 결정: {decision}"
            if reason:
                msg += f" ({reason[:80]})"
            self._print(msg)

        elif event.type == EventType.STAGE_STARTED:
            stage = str(event.data.get("stage", "stage"))
            description = str(event.data.get("description", "") or "").strip()
            if description:
                self._print(f"\n── {stage}: {description} ──")
            else:
                self._print(f"\n── {stage} ──")

        elif event.type == EventType.STAGE_COMPLETED:
            stage = str(event.data.get("stage", "stage"))
            duration = event.data.get("duration_sec")
            if duration is not None:
                self._print(f"── {stage} 완료 ({float(duration):.1f}s) ──")
            else:
                self._print(f"── {stage} 완료 ──")

        elif event.type == EventType.PHASE_STARTED:
            phase = str(event.data.get("phase", "phase"))
            description = str(event.data.get("description", "") or "").strip()
            self._print(f"\n{'='*50}")
            if description:
                self._print(f"  Phase: {phase} — {description}")
            else:
                self._print(f"  Phase: {phase}")
            self._print(f"{'='*50}")

        elif event.type == EventType.PHASE_COMPLETED:
            phase = str(event.data.get("phase", "phase"))
            duration = event.data.get("duration_sec")
            if duration is not None:
                self._print(f"{'='*50}\n  Phase {phase} 완료 ({float(duration):.1f}s)\n{'='*50}")
            else:
                self._print(f"{'='*50}\n  Phase {phase} 완료\n{'='*50}")

        elif event.type == EventType.WORKER_SPAWNED:
            worker_id = str(event.data.get("worker_id", "worker"))
            role = str(event.data.get("role", "") or "").strip()
            label = f"{worker_id}" + (f" ({role})" if role else "")
            self._print(f"  + worker: {label}")

        elif event.type == EventType.WORKER_COMPLETED:
            worker_id = str(event.data.get("worker_id", "worker"))
            passed = event.data.get("passed")
            duration = event.data.get("duration_sec")
            icon = "✓" if passed else "✗" if passed is not None else "·"
            parts = []
            if duration is not None:
                parts.append(f"{float(duration):.1f}s")
            detail = f" ({', '.join(parts)})" if parts else ""
            self._print(f"  {icon} worker {worker_id}{detail}")

        elif event.type == EventType.TEAM_TURN_STARTED:
            turn = event.data.get("turn", "?")
            self._print(f"\n── team turn {turn} ──")

        elif event.type == EventType.TEAM_TURN_COMPLETED:
            turn = event.data.get("turn", "?")
            duration = event.data.get("duration_sec")
            workers = event.data.get("worker_count", "?")
            detail = f" ({float(duration):.1f}s, {workers} workers)" if duration is not None else ""
            self._print(f"── team turn {turn} 완료{detail} ──")

        elif event.type in {EventType.ARTIFACT_CREATED, EventType.ARTIFACT_UPDATED}:
            kind = str(event.data.get("kind", "artifact"))
            path = str(event.data.get("path", "?"))
            # Shorten path for readability
            from pathlib import Path
            try:
                short = Path(path).name
            except Exception:
                short = path
            self._print(f"    📄 {kind}: {short}")

        elif event.type == EventType.GATE_EVALUATED:
            gate = str(event.data.get("gate", "gate"))
            passed = bool(event.data.get("passed", False))
            icon = "✓" if passed else "✗"
            self._print(f"  {icon} Gate {gate}: {'PASS' if passed else 'FAIL'}")

    def _print(self, text: str) -> None:
        print(text, file=self.stream)

    def _is_tty(self) -> bool:
        return hasattr(self.stream, "isatty") and self.stream.isatty()
