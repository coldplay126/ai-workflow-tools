## G2 Gate 판정 기준

아래 조건을 모두 만족하면 PASS:

1. **CRITICAL finding 0건**: `findings.count(severity=CRITICAL) == 0`
2. **HIGH 이슈 해소**: HIGH severity 이슈에 resolution 또는 user acknowledgment
3. **커버리지 80% 이상**: `coverage.percentage >= 80`
4. **REVIEW_CONFLICT 없음**: multi-LLM 교차 검증 시 `REVIEW_CONFLICT count(severity>=HIGH) == 0`

FAIL 시 반환할 필드:
- conclusion: "FAIL — {사유}"
- findings: severity별 이슈 목록
- coverage: total_requirements, mapped_requirements, percentage, gaps
- action_items: 수정 제안
