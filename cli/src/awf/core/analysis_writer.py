"""Writer/Judge schema and parsing for Analysis v3 Phase 2.

Defines the Claim and WriterResult dataclasses per Technical Specs §3.1,
and provides functions to parse raw LLM output into structured results
and merge claims from multiple Writers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from awf.core.analysis_outputs import FILE_MARKER_RE


VALID_CLAIM_TYPES = {"endpoint", "table", "external_call", "business_logic", "signal", "finding"}
VALID_CONFIDENCE_LEVELS = {"high", "medium", "low"}


@dataclass
class Claim:
    id: str
    type: str
    claim: str
    evidence: str
    source_files: list[str]
    confidence: str  # high|medium|low

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "claim": self.claim,
            "evidence": self.evidence,
            "source_files": self.source_files,
            "confidence": self.confidence,
        }


@dataclass
class WriterResult:
    writer: str  # structure|behavior
    claims: list[Claim]
    output_sections: dict[str, str]  # file_name -> content
    raw_output: str

    def low_confidence_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.confidence == "low"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "writer": self.writer,
            "claims": [c.to_dict() for c in self.claims],
            "output_sections": self.output_sections,
        }


@dataclass
class JudgeResult:
    verdict: str  # merged
    merged_claims: list[dict[str, Any]]
    merged_output: dict[str, str]  # file_name -> content
    consistency_checks: list[dict[str, Any]]
    code_fallback_files: list[str] = field(default_factory=list)
    raw_output: str = ""

    def has_fallback(self) -> bool:
        return len(self.code_fallback_files) > 0


# --- Parsing ---

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n\s*```", re.DOTALL)


def _extract_json_block(raw: str) -> dict[str, Any] | None:
    """Extract the first ```json ... ``` block from raw text."""
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _parse_claim(data: dict[str, Any]) -> Claim:
    return Claim(
        id=str(data.get("id", "")),
        type=str(data.get("type", "")),
        claim=str(data.get("claim", "")),
        evidence=str(data.get("evidence", "")),
        source_files=list(data.get("source_files", [])),
        confidence=str(data.get("confidence", "medium")),
    )


def parse_writer_output(raw: str, writer_id: str) -> WriterResult:
    """Parse Writer LLM response into WriterResult.

    Expected format:
    1. ```json block with claims array (or full WriterResult JSON)
    2. ===FILE: name=== markers with output_sections
    """
    claims: list[Claim] = []
    output_sections: dict[str, str] = {}

    # Extract claims from JSON block
    json_data = _extract_json_block(raw)
    if json_data:
        claims_raw = json_data.get("claims", [])
        if isinstance(claims_raw, list):
            claims = [_parse_claim(c) for c in claims_raw if isinstance(c, dict)]
    elif raw.strip():
        import sys
        print(
            f"warning: writer '{writer_id}' returned no valid JSON claims block; "
            f"output length={len(raw)} chars",
            file=sys.stderr,
        )

    # Extract output_sections from ===FILE: markers
    matches = list(FILE_MARKER_RE.finditer(raw))
    for index, match in enumerate(matches):
        name = match.group("name").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        content = raw[start:end].strip()
        if content:
            output_sections[name] = content + "\n"

    return WriterResult(
        writer=writer_id,
        claims=claims,
        output_sections=output_sections,
        raw_output=raw,
    )


def parse_judge_output(raw: str, required_files: list[str] | None = None) -> JudgeResult | None:
    """Parse Judge LLM response into JudgeResult.

    Expected format:
    1. ```json block with verdict, merged_claims, consistency_checks, code_fallback_files
    2. ===FILE: markers for merged_output
    """
    verdict = "merged"
    merged_claims: list[dict[str, Any]] = []
    consistency_checks: list[dict[str, Any]] = []
    code_fallback_files: list[str] = []

    json_data = _extract_json_block(raw)
    if json_data:
        verdict = str(json_data.get("verdict", "merged"))
        merged_claims = list(json_data.get("merged_claims", []))
        consistency_checks = list(json_data.get("consistency_checks", []))
        code_fallback_files = list(json_data.get("code_fallback_files", []))

    # Extract merged_output from FILE markers
    merged_output: dict[str, str] = {}
    matches = list(FILE_MARKER_RE.finditer(raw))
    for index, match in enumerate(matches):
        name = match.group("name").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        content = raw[start:end].strip()
        if content:
            merged_output[name] = content + "\n"

    # Validate all required output files are present
    _required = required_files if required_files is not None else None
    if _required is None:
        from awf.core.analysis_state import REQUIRED_OUTPUT_FILES
        _required = REQUIRED_OUTPUT_FILES
    missing_files = [f for f in _required if f not in merged_output]
    if missing_files:
        import sys
        print(
            f"warning: judge output missing required files: {missing_files}, triggering fallback",
            file=sys.stderr,
        )
        return None

    # Deduplicate code_fallback_files
    seen_fallback: set[str] = set()
    deduped_fallback: list[str] = []
    for f in code_fallback_files:
        if f not in seen_fallback:
            seen_fallback.add(f)
            deduped_fallback.append(f)

    return JudgeResult(
        verdict=verdict,
        merged_claims=merged_claims,
        merged_output=merged_output,
        consistency_checks=consistency_checks,
        code_fallback_files=deduped_fallback[:3],  # max 3 files per §2.3
        raw_output=raw,
    )


def merge_claims(results: list[WriterResult]) -> list[dict[str, Any]]:
    """Merge claims from multiple Writers for Judge input.

    Deduplicates by (type, claim) key. When duplicates found,
    keeps both with original writer prefix for Judge to resolve.
    """
    seen: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for wr in results:
        for c in wr.claims:
            key = (c.type, c.claim)
            entry = {**c.to_dict(), "writer": wr.writer}
            if key not in seen:
                seen[key] = []
            seen[key].append(entry)

    merged: list[dict[str, Any]] = []
    for entries in seen.values():
        if len(entries) == 1:
            merged.append(entries[0])
        else:
            # Multiple writers made same claim - include all for Judge
            for entry in entries:
                merged.append(entry)
    return merged


def build_judge_input(results: list[WriterResult]) -> str:
    """Build the Judge prompt input section from WriterResults."""
    parts: list[str] = []
    for wr in results:
        parts.append(f"## Writer: {wr.writer}")
        parts.append("")
        parts.append("### Claims")
        parts.append("```json")
        parts.append(json.dumps([c.to_dict() for c in wr.claims], ensure_ascii=False, indent=2))
        parts.append("```")
        parts.append("")
        parts.append("### Output Sections")
        for section_name, content in wr.output_sections.items():
            parts.append(f"#### {section_name}")
            parts.append(content.rstrip())
            parts.append("")
    return "\n".join(parts)


def validate_evidence_integrity(
    writer_results: list[WriterResult],
    judge_result: JudgeResult,
) -> list[str]:
    """Validate that Judge preserved Writer evidence (A5 invariant).

    Checks that merged_claims' evidence and source_files fields
    match the original Writer claims. Returns list of violation descriptions.
    Empty list means integrity is maintained.
    """
    # Build index: bare/qualified claim_id -> (evidence, source_files)
    original: dict[str, tuple[str, tuple[str, ...]]] = {}
    for writer_result in writer_results:
        for claim in writer_result.claims:
            if not claim.id:
                continue
            value = (claim.evidence, tuple(claim.source_files))
            original[claim.id] = value
            original[f"{writer_result.writer}:{claim.id}"] = value

    violations: list[str] = []
    for merged_claim in judge_result.merged_claims:
        claim_id = str(merged_claim.get("id", ""))
        references_raw = merged_claim.get("original_claims")
        references = (
            [str(reference) for reference in references_raw if str(reference)]
            if isinstance(references_raw, list)
            else []
        )
        legacy_direct_claim = not references
        if references:
            for reference in references:
                if reference not in original:
                    violations.append(f"unknown_claim_id:{reference}")
            if len(references) != 1 or references[0] not in original:
                continue
            original_id = references[0]
        else:
            if not claim_id:
                continue
            if claim_id not in original:
                violations.append(f"unknown_claim_id:{claim_id}")
                continue
            original_id = claim_id

        original_evidence, original_sources = original[original_id]
        if (
            legacy_direct_claim or "evidence" in merged_claim
        ) and str(merged_claim.get("evidence", "")) != original_evidence:
            violations.append(f"evidence_modified:{claim_id}")
        if (
            legacy_direct_claim or "source_files" in merged_claim
        ) and tuple(merged_claim.get("source_files", [])) != original_sources:
            violations.append(f"source_files_modified:{claim_id}")

    return violations
