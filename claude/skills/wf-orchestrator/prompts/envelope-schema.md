## Worker Result Envelope

작업 결과를 반드시 아래 JSON 형식으로 반환하세요:

```json
{
  "status": "completed | escaped | failed",
  "phase": "<현재 Phase명>",
  "provider": "<사용된 provider명>",
  "result": {
    "conclusion": "PASS | FAIL — 사유",
    "findings": [
      {
        "id": "F1",
        "severity": "CRITICAL | HIGH | MEDIUM | LOW",
        "category": "coverage_gap | inconsistency | scope_violation | ambiguity",
        "locations": ["file:line"],
        "summary": "발견 내용",
        "recommendation": "권장 조치"
      }
    ],
    "evidence": [{"id": "E1", "detail": "근거"}],
    "risks": [{"id": "R1", "severity": "HIGH | MEDIUM | LOW", "detail": "리스크"}],
    "action_items": [{"id": "A1", "action": "조치 항목"}],
    "coverage": {
      "total_requirements": 0,
      "mapped_requirements": 0,
      "percentage": 0,
      "gaps": []
    }
  },
  "escape": null,
  "meta": {"format_version": 1}
}
```

- `status: completed` → 정상 완료, `result` 필드에 분석 결과
- `status: escaped` → 정상 완료 불가, `escape` 필드에 탈출 사유
- `status: failed` → 오류 발생
