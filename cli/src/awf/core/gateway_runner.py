from __future__ import annotations

import time

from awf.core.events import EventType
from awf.core.event_processor import EventProcessor
from awf.core.progress import ProgressDisplay
from awf.core.task import TaskDefinition
from awf.providers.base import ProviderResult


def run_native_provider_task(
    *,
    provider,
    task: TaskDefinition,
    processor: EventProcessor | None = None,
) -> tuple[ProviderResult, float]:
    processor = processor or EventProcessor(handlers=[ProgressDisplay().handle])
    started_at = time.monotonic()
    output_chunks: list[str] = []
    stderr_chunks: list[str] = []
    returncode = 0
    stream = provider.execute(task)
    try:
        for event in stream:
            processor.record(event)
            if event.type == EventType.PROVIDER_OUTPUT:
                text = str(event.data.get("text", "") or "")
                if text:
                    output_chunks.append(text)
            elif event.type == EventType.TASK_FAILED:
                returncode = int(event.data.get("returncode", 1) or 1)
                error = str(event.data.get("error", "") or "")
                if error:
                    stderr_chunks.append(error)
            elif event.type == EventType.TASK_COMPLETED:
                returncode = int(event.data.get("returncode", 0) or 0)
                result_text = str(event.data.get("result", "") or "")
                if result_text and not output_chunks:
                    output_chunks.append(result_text)
    except KeyboardInterrupt:
        stream.cancel()
        elapsed_at_cancel = time.monotonic() - started_at
        import sys
        print(
            f"gateway_runner: cancelled after {elapsed_at_cancel:.1f}s, "
            f"collected {len(output_chunks)} output chunks",
            file=sys.stderr,
        )
        raise
    elapsed = time.monotonic() - started_at
    return (
        ProviderResult(
            returncode=returncode,
            stdout="".join(output_chunks),
            stderr="\n".join(stderr_chunks),
        ),
        elapsed,
    )
