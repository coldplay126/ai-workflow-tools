from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def workflow_result_envelope_schema(phase: str) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AWF workflow worker result envelope",
        "type": "object",
        "additionalProperties": True,
        "required": ["status", "result"],
        "properties": {
            "status": {"type": "string", "enum": ["completed", "escaped", "failed"]},
            "phase": {"type": "string", "const": phase},
            "provider": {"type": "string"},
            "result": {"type": "object", "additionalProperties": True},
            "escape": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "severity": {"type": "string"},
                            "reason": {"type": "string"},
                            "summary": {"type": "string"},
                            "evidence": {"type": "array"},
                            "affected_files": {"type": "array"},
                            "recommended_action": {"type": "string"},
                        },
                    },
                ]
            },
            "meta": {"type": "object", "additionalProperties": True},
        },
    }


def workflow_result_envelope_schema_json(phase: str) -> str:
    return json.dumps(workflow_result_envelope_schema(phase), ensure_ascii=False)


def write_temp_schema_file(schema: dict[str, Any], *, prefix: str) -> str:
    handle = tempfile.NamedTemporaryFile(prefix=prefix, suffix=".json", mode="w", encoding="utf-8", delete=False)
    try:
        json.dump(schema, handle, ensure_ascii=False)
        handle.write("\n")
    finally:
        handle.close()
    return str(Path(handle.name))
