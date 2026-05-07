from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from awf.core.events import EventType, ExecutionEvent


@dataclass
class ArtifactManager:
    created_count: int = 0
    updated_count: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)
    by_task: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def handle(self, event: ExecutionEvent) -> None:
        if event.type not in {EventType.ARTIFACT_CREATED, EventType.ARTIFACT_UPDATED}:
            return
        kind = str(event.data.get("kind", "artifact"))
        path = str(event.data.get("path", ""))
        self.by_kind[kind] += 1
        if path:
            self.by_task[event.task_id].append(path)
        if event.type == EventType.ARTIFACT_CREATED:
            self.created_count += 1
        else:
            self.updated_count += 1

    def summary(self) -> dict[str, object]:
        return {
            "created": self.created_count,
            "updated": self.updated_count,
            "byKind": dict(self.by_kind),
            "byTask": dict(self.by_task),
        }
