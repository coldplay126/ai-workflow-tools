## G5 Gate 판정 기준

아래 조건을 모두 만족하면 PASS:

1. **스코프 준수**: `awf wf scope-check`(결정론) 종료 코드 0 — 변경된 파일이 모두 `planned_files ∪ expanded_files`에 속함. `.workflow/` 경로는 자동 제외.
2. **Spec 충족**: spec.md의 Success Criteria가 모두 검증 가능
3. **SCOPE_VIOLATION 없음**: scope-check가 보고한 `violation` 분류 0건
4. **아키텍처 이슈 없음**: 구조적 문제 없음

FAIL 시 반환할 필드:
- conclusion: "FAIL — {사유}"
- findings: scope_violation, arch_issue 등 (각 violation은 scope-check의 `reason`을 그대로 인용)
- risks: 회귀 리스크
