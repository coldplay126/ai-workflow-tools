## G5 Gate 판정 기준

아래 조건을 모두 만족하면 PASS:

1. **스코프 준수**: 변경된 파일이 allowed-files.json 범위 내
2. **Spec 충족**: spec.md의 Success Criteria가 모두 검증 가능
3. **SCOPE_VIOLATION 없음**: 허용 범위 밖 파일 변경 없음
4. **아키텍처 이슈 없음**: 구조적 문제 없음

FAIL 시 반환할 필드:
- conclusion: "FAIL — {사유}"
- findings: scope_violation, arch_issue 등
- risks: 회귀 리스크
