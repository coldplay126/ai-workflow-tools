"""Unit tests for analysis_writer.py — Phase 2 Writer/Judge parsing."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.analysis_writer import (
    Claim,
    WriterResult,
    JudgeResult,
    parse_writer_output,
    parse_judge_output,
    merge_claims,
    build_judge_input,
)


# --- parse_writer_output ---

def test_parse_writer_output_normal():
    raw = '''Here is the analysis:

```json
{
  "writer": "structure",
  "claims": [
    {"id": "S1", "type": "endpoint", "claim": "POST /attendance/check", "evidence": "controller.ts:15", "source_files": ["src/attendance/controller.ts"], "confidence": "high"},
    {"id": "S2", "type": "table", "claim": "T_ATTENDANCE read/write", "evidence": "service.ts:42", "source_files": ["src/attendance/service.ts"], "confidence": "medium"}
  ]
}
```

===FILE: api-spec.json===
{
  "domain": "attendance",
  "endpoints": [{"method": "POST", "path": "/attendance/check"}]
}

===FILE: data-model.md===
# 데이터 모델
## T_ATTENDANCE
| 컬럼 | 타입 |
'''
    result = parse_writer_output(raw, "structure")
    assert result.writer == "structure"
    assert len(result.claims) == 2
    assert result.claims[0].id == "S1"
    assert result.claims[0].type == "endpoint"
    assert result.claims[0].confidence == "high"
    assert result.claims[1].confidence == "medium"
    assert "api-spec.json" in result.output_sections
    assert "data-model.md" in result.output_sections
    assert '"attendance"' in result.output_sections["api-spec.json"]


def test_parse_writer_output_no_claims():
    raw = '''===FILE: api-spec.json===
{"domain": "test", "endpoints": []}

===FILE: data-model.md===
# Empty
'''
    result = parse_writer_output(raw, "structure")
    assert result.writer == "structure"
    assert len(result.claims) == 0
    assert len(result.output_sections) == 2


def test_parse_writer_output_no_sections():
    raw = '''```json
{
  "writer": "behavior",
  "claims": [
    {"id": "B1", "type": "business_logic", "claim": "흐름 A", "evidence": "svc.ts:10", "source_files": ["svc.ts"], "confidence": "low"}
  ]
}
```
'''
    result = parse_writer_output(raw, "behavior")
    assert len(result.claims) == 1
    assert result.claims[0].confidence == "low"
    assert len(result.output_sections) == 0


def test_low_confidence_claims():
    wr = WriterResult(
        writer="structure",
        claims=[
            Claim("S1", "endpoint", "a", "b", ["f.ts"], "high"),
            Claim("S2", "table", "c", "d", ["g.ts"], "low"),
            Claim("S3", "table", "e", "f", ["h.ts"], "low"),
        ],
        output_sections={},
        raw_output="",
    )
    low = wr.low_confidence_claims()
    assert len(low) == 2
    assert all(c.confidence == "low" for c in low)


# --- parse_judge_output ---

def test_parse_judge_output_normal():
    raw = '''판정 결과:

```json
{
  "verdict": "merged",
  "merged_claims": [
    {"id": "M1", "original_claims": ["structure:S1"], "resolution": "selected", "conflict_note": null}
  ],
  "consistency_checks": [
    {"check": "endpoint ↔ flow", "result": "pass", "detail": null}
  ],
  "code_fallback_files": []
}
```

===FILE: api-spec.json===
{"domain": "attendance", "endpoints": []}

===FILE: data-model.md===
# Model

===FILE: domain-overview.md===
# Overview

===FILE: external-integration.md===
# Integration
'''
    result = parse_judge_output(raw)
    assert result.verdict == "merged"
    assert len(result.merged_claims) == 1
    assert len(result.consistency_checks) == 1
    assert result.code_fallback_files == []
    assert not result.has_fallback()
    assert len(result.merged_output) == 4


def test_parse_judge_output_with_fallback():
    raw = '''```json
{
  "verdict": "merged",
  "merged_claims": [],
  "consistency_checks": [],
  "code_fallback_files": ["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"]
}
```

===FILE: api-spec.json===
{}
'''
    result = parse_judge_output(raw, required_files=["api-spec.json"])
    assert result.has_fallback()
    assert len(result.code_fallback_files) == 3  # max 3 per §2.3


# --- merge_claims ---

def test_merge_claims_dedup():
    wr1 = WriterResult(
        writer="structure",
        claims=[
            Claim("S1", "external_call", "Redis.get()", "a.ts:10", ["a.ts"], "high"),
        ],
        output_sections={},
        raw_output="",
    )
    wr2 = WriterResult(
        writer="behavior",
        claims=[
            Claim("B1", "external_call", "Redis.get()", "a.ts:10", ["a.ts"], "medium"),
            Claim("B2", "business_logic", "흐름 X", "b.ts:20", ["b.ts"], "high"),
        ],
        output_sections={},
        raw_output="",
    )
    merged = merge_claims([wr1, wr2])
    # Redis.get() appears in both → both kept for Judge
    external_calls = [m for m in merged if m["type"] == "external_call"]
    assert len(external_calls) == 2
    assert {e["writer"] for e in external_calls} == {"structure", "behavior"}
    # business_logic unique
    biz = [m for m in merged if m["type"] == "business_logic"]
    assert len(biz) == 1


# --- build_judge_input ---

def test_build_judge_input_format():
    wr = WriterResult(
        writer="structure",
        claims=[Claim("S1", "endpoint", "POST /test", "c.ts:1", ["c.ts"], "high")],
        output_sections={"api-spec.json": '{"endpoints": []}'},
        raw_output="",
    )
    text = build_judge_input([wr])
    assert "## Writer: structure" in text
    assert "### Claims" in text
    assert "```json" in text
    assert "### Output Sections" in text
    assert "api-spec.json" in text


# --- Run ---

def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    failed = 0
    for test_fn in tests:
        name = test_fn.__name__
        try:
            test_fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
