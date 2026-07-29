from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)
    return value


def write_omp_dispatch_provenance(
    repo_root: str | os.PathLike[str],
    *,
    strategy: str,
    mode: str,
    agents: Iterable[Any],
    elapsed_sec: float,
) -> Path | None:
    """Write one redacted OMP dispatch record under workflow artifacts.

    Prompts and response bodies are intentionally excluded. Their hashes retain
    correlation value without copying potentially sensitive model content.
    """
    root = Path(repo_root).resolve()
    workflow_dir = root / ".workflow"
    if not workflow_dir.is_dir():
        return None

    records: list[dict[str, Any]] = []
    for agent in agents:
        stdout = str(getattr(agent, "stdout", "") or "")
        metadata = _json_safe(getattr(agent, "metadata", {}) or {})
        records.append(
            {
                "role": str(getattr(agent, "role", "")),
                "provider": str(getattr(agent, "provider_name", "")),
                "returncode": int(getattr(agent, "returncode", 1)),
                "timed_out": bool(getattr(agent, "timed_out", False)),
                "parse_error": bool(getattr(agent, "parse_error", False)),
                "elapsed_sec": round(float(getattr(agent, "elapsed_sec", 0.0)), 3),
                "conclusion": str(getattr(agent, "conclusion", "")),
                "output_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                "metadata": metadata,
            }
        )

    now = datetime.now(timezone.utc)
    run_id = f"omp-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": now.isoformat(),
        "backend": "omp",
        "strategy": strategy,
        "mode": mode,
        "elapsed_sec": round(elapsed_sec, 3),
        "agents": records,
    }

    target_dir = workflow_dir / "artifacts" / "dispatch"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{run_id}.json"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target_dir,
        prefix=f".{run_id}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(encoded)
        temp_path = Path(handle.name)
    os.replace(temp_path, target)
    return target
