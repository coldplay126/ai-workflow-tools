from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from awf.providers.base import ProviderCapability, ProviderResult, TokenUsage

FANOUT_FILES = [
    "api-spec.json",
    "data-model.md",
    "domain-overview.md",
    "external-integration.md",
]


class FixtureProvider:
    name = "fixture"
    capabilities = {ProviderCapability.COMPLETE}

    def __init__(self, result_file: str) -> None:
        resolved = os.environ.get("AWF_FIXTURE_RESULT_FILE", "") or result_file
        self._result_file_raw = resolved
        self.result_file = Path(resolved).expanduser().resolve()

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        # timeout_sec is accepted for contract parity; tests may inject a deterministic delay.
        _ = timeout_sec
        try:
            delay_sec = max(0.0, float(os.environ.get("AWF_FIXTURE_DELAY_SEC", "0") or 0))
        except ValueError:
            delay_sec = 0.0
        if delay_sec:
            time.sleep(delay_sec)
        # Re-resolve result_file relative to cwd if the original was relative
        if cwd and not Path(self._result_file_raw).is_absolute():
            resolved = (Path(cwd) / self._result_file_raw).resolve()
            if resolved.is_file():
                self.result_file = resolved
        forced_returncode = int(os.environ.get("AWF_FIXTURE_RETURNCODE", "0") or 0)
        usage_json = os.environ.get("AWF_FIXTURE_USAGE_JSON", "").strip()
        usage = None
        if usage_json:
            try:
                parsed = json.loads(usage_json)
                usage = TokenUsage(
                    input_tokens=int(parsed.get("input_tokens", 0) or 0),
                    output_tokens=int(parsed.get("output_tokens", 0) or 0),
                )
            except Exception:
                usage = None
        chat_summary = os.environ.get("AWF_FIXTURE_CHAT_SUMMARY", "").strip()
        if chat_summary and "Summarize the earlier chat turns for compaction." in prompt:
            return ProviderResult(
                returncode=forced_returncode,
                stdout=chat_summary,
                stderr="",
                usage=usage,
            )
        fanout_dir = os.environ.get("AWF_FIXTURE_FANOUT_DIR", "").strip()
        if fanout_dir:
            resolved_dir = Path(fanout_dir).expanduser().resolve()

            # v2 Writer/Judge pattern detection
            # Order matters: check Behavior before Structure since Behavior prompt mentions "Structure Writer"
            if "**Behavior Writer**" in prompt:
                return _build_v2_writer_fixture(resolved_dir, "behavior", ["domain-overview.md", "external-integration.md"], forced_returncode, usage)
            if "**Structure Writer**" in prompt:
                return _build_v2_writer_fixture(resolved_dir, "structure", ["api-spec.json", "data-model.md"], forced_returncode, usage)
            if "**Judge**" in prompt and "merged_claims" in prompt:
                return _build_v2_judge_fixture(resolved_dir, forced_returncode, usage)

            # v1 legacy pattern
            if "You are the synthesizer." in prompt:
                synth_file = os.environ.get("AWF_FIXTURE_FANOUT_SYNTHESIZER_FILE", "").strip()
                if synth_file:
                    synth_path = Path(synth_file).expanduser().resolve()
                    if synth_path.exists():
                        return ProviderResult(
                            returncode=forced_returncode,
                            stdout=synth_path.read_text(encoding="utf-8"),
                            stderr="",
                            usage=usage,
                        )
                return ProviderResult(
                    returncode=forced_returncode,
                    stdout="# Fan-out Synthesizer Plan\n\n- Keep outputs consistent.\n",
                    stderr="",
                    usage=usage,
                )
            for file_name in FANOUT_FILES:
                if f"`{file_name}`" in prompt or f"file: {file_name}" in prompt:
                    target = resolved_dir / file_name
                    if target.exists():
                        return ProviderResult(
                            returncode=forced_returncode,
                            stdout=target.read_text(encoding="utf-8"),
                            stderr="",
                            usage=usage,
                        )
                    return ProviderResult(returncode=2, stdout="", stderr=f"Fixture fan-out file not found: {target}")
        if not self.result_file.exists():
            return ProviderResult(
                returncode=2,
                stdout="",
                stderr=f"Fixture result file not found: {self.result_file}",
            )
        return ProviderResult(
            returncode=forced_returncode,
            stdout=self.result_file.read_text(encoding="utf-8"),
            stderr="",
            usage=usage,
        )


def _build_v2_writer_fixture(
    fanout_dir: Path,
    writer_id: str,
    file_names: list[str],
    returncode: int,
    usage: "TokenUsage | None",
) -> ProviderResult:
    """Generate a v2 Writer fixture response with claims + FILE markers."""
    import json as _json

    prefix = "S" if writer_id == "structure" else "B"
    claims: list[dict] = []
    sections: list[str] = []

    for i, file_name in enumerate(file_names, start=1):
        target = fanout_dir / file_name
        content = target.read_text(encoding="utf-8") if target.exists() else f"# {file_name} (fixture)\n"
        claim_type = "endpoint" if "api-spec" in file_name else "table" if "data-model" in file_name else "business_logic" if "domain" in file_name else "external_call"
        claims.append({
            "id": f"{prefix}{i}",
            "type": claim_type,
            "claim": f"Fixture claim for {file_name}",
            "evidence": "fixture:1",
            "source_files": ["fixture.ts"],
            "confidence": "high",
        })
        sections.append(f"===FILE: {file_name}===")
        sections.append(content.rstrip())
        sections.append("")

    json_block = _json.dumps({"writer": writer_id, "claims": claims}, ensure_ascii=False, indent=2)
    output = f"```json\n{json_block}\n```\n\n" + "\n".join(sections)
    return ProviderResult(returncode=returncode, stdout=output, stderr="", usage=usage)


def _build_v2_judge_fixture(
    fanout_dir: Path,
    returncode: int,
    usage: "TokenUsage | None",
) -> ProviderResult:
    """Generate a v2 Judge fixture response with merged output."""
    import json as _json

    judge_json = {
        "verdict": "merged",
        "merged_claims": [
            {"id": "M1", "original_claims": ["structure:S1"], "resolution": "selected", "conflict_note": None},
        ],
        "consistency_checks": [
            {"check": "endpoint ↔ flow", "result": "pass", "detail": None},
            {"check": "table ↔ integration", "result": "pass", "detail": None},
            {"check": "external_call 정합성", "result": "pass", "detail": None},
        ],
        "code_fallback_files": [],
    }
    json_block = _json.dumps(judge_json, ensure_ascii=False, indent=2)

    sections: list[str] = []
    for file_name in FANOUT_FILES:
        target = fanout_dir / file_name
        content = target.read_text(encoding="utf-8") if target.exists() else f"# {file_name} (judge fixture)\n"
        sections.append(f"===FILE: {file_name}===")
        sections.append(content.rstrip())
        sections.append("")

    output = f"```json\n{json_block}\n```\n\n" + "\n".join(sections)
    return ProviderResult(returncode=returncode, stdout=output, stderr="", usage=usage)
