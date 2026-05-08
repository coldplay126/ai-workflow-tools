# Multi-Agent Reference

운영값, 비용표, 키워드 목록, 타임아웃 등 `docs/patterns/multi-agent/`에서 분리된 구현 세부와 운영값.

---

## 0. 에이전트 협업 패턴 상세

### 패턴 비교

| | 서브에이전트 | 에이전트 팀 | A2A |
|---|---|---|---|
| **관계** | 부모→자식 (위임) | 동료 (협업) | 독립 서비스 (계약) |
| **통신** | 결과만 부모에게 반환 | 공유 태스크 리스트 + 직접 메시지 | HTTP 기반 표준 프로토콜 |
| **컨텍스트** | 부모 세션 내 실행 | 각자 독립 컨텍스트 | 완전 분리된 시스템 |
| **조율** | 부모가 전부 관리 | 자율 조율 (태스크 자기 할당) | Agent Card 발견 + 메시지 교환 |
| **비용** | 낮음 (결과 요약) | 높음 (세션당 전체 컨텍스트) | 시스템에 따라 다름 |
| **상태** | 구현됨 (Agent 도구) | 실험적 (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS) | 미도입 |

### 에이전트 팀 도입 후보

| 구간 | 왜 팀이 필요한가 | 기대 효과 |
|------|----------------|----------|
| WF 기획서 설계 (spec-kit) | constitution → specify → plan → test 각 단계에서 교차 검증과 반박 | spec 모순/누락 조기 발견 |
| QA | happy path 검증 + 적극적 파괴 시도를 병렬로 | 확증 편향 방지, 엣지/코너 케이스 탐색 |

### 파이프라인별 적용

| 파이프라인 | 구간 | 패턴 | 이유 |
|-----------|------|------|------|
| Analysis | Stage 1 파일별 관찰 | 서브에이전트 | 상호 통신 불필요, 결과만 수집 |
| Analysis | Stage 2 Writer 병렬 | 서브에이전트 | Writer 간 독립, Analysis Judge가 통합 |
| Workflow | review/verify | 서브에이전트 | 독립 평가 후 판정 |
| Workflow | 기획서 설계 (spec-kit) | 에이전트 팀 | 서로 다른 관점에서 spec 교차 검증과 반박 |
| Workflow | QA | 에이전트 팀 | 경쟁적 엣지/코너 케이스 탐색 |
| Multi-Agent | #cross, #critical | 서브에이전트 | 독립 분석 후 Multi-Agent Judge 통합 |

### 에이전트 팀이 유효한 조건

- 에이전트 간 **토론/반박**이 단일 에이전트보다 높은 품질을 기대할 수 있을 때
- 작업이 **경쟁 가설** 구조일 때 (한쪽이 만들고, 다른 쪽이 깨뜨리려 시도)
- **확증 편향 방지**가 중요할 때 (QA, 보안 검증, 설계 리뷰)

### A2A 도입 시점

외부 CI/CD, 별도 서버의 분석 에이전트, 다른 팀의 리뷰 에이전트와 연동이 필요할 때.
현재는 같은 머신에서 실행되므로 불필요.

---

## 1. 모드별 비교표

| 모드 | 에이전트 수 | 실행 방식 | 상대 비용 | 신뢰도 | 대표 용도 |
|------|-----------|----------|----------|--------|----------|
| solo | 1 | 단독 | 1x | 낮음 | 일반 작업, 단순 변경 |
| quick | 1 | 읽기 전용 | 0.5x | 낮음 | 대량 조회, 구조 파악 |
| precise | 2 | 순차 | 2x | 중간 | 코드 분석, 설정 검토 |
| cross | 2 + Judge | 병렬 | 2.5x | 높음 | 고위험 변경, 교차 검증 |
| critical | 3 + Judge | 순차 체인 | 3.5x | 최고 | 프로덕션 배포, 데이터 삭제 |

---

## 2. 승격/강등 키워드

### 자동 승격

| 승격 전 | 승격 후 | 키워드 |
|---------|---------|--------|
| solo | cross | security, auth, IAM, credential, token, permission, rbac, acl, secret, certificate, ssl, tls, firewall, waf, sg, security-group, 인증, 인가, 보안 |
| solo | critical | production, deploy, rollback, delete, migration, drop, truncate, prod, 프로덕션 |

### 자동 강등

| 키워드 | 동작 |
|--------|------|
| readme, doc, documentation, 문서 | solo로 강등 |
| typo, comment, 주석, 오타 | solo로 강등 |
| format, lint, style | solo로 강등 |

### 실행 중 강등

| 상황 | 강등 경로 |
|------|----------|
| 보조 에이전트 타임아웃 | 다음 fallback 모드 |
| JSON 파싱 실패 | 다음 fallback 모드 |
| 전체 에이전트 실패 | solo |

---

## 3. Fallback 체인

| 원본 모드 | Fallback 순서 |
|----------|-------------|
| critical | cross → precise → solo |
| cross | precise → solo |
| precise | solo |
| quick | solo |
| solo | (없음, 최종 모드) |

---

## 4. 타임아웃

| 모드 | 에이전트 | 기본 타임아웃 |
|------|---------|-------------|
| quick | Secondary | 45s |
| precise | Secondary | 90s |
| precise | Primary | 120s |
| cross | Secondary A | 90s |
| cross | Secondary B | 90s |
| critical | Secondary A (codex) | 90s + 30s warmup grace (cmux 백엔드 시) |
| critical | Secondary B (sonnet) | 60s + 30s warmup grace (cmux 백엔드 시) |
| critical | Primary | 120s + 30s warmup grace (cmux 백엔드 시) |

> critical 모드는 `MultiAgentDispatch.run_chained` 인터페이스로 실행되며, cmux 백엔드 선택 시 step 마다 같은 role 의 worker 가 고정 재사용된다. warmup grace 는 fresh worker spawn 시 cmux 탭 + AI CLI 부팅 시간을 흡수하기 위한 보정.

---

## 5. Confidence Tie-Breaking

| 요소 | 가중치 |
|------|--------|
| Finding 수 | 0.3 |
| Evidence 수 | 0.3 |
| 응답 시간 비율 | 0.2 |
| 프로토콜 적합도 | 0.2 |
| 적용 임계값 | 0.3 |

---

## 6. 프로토콜 종류

| 프로토콜 | 역할 | 사용 모드 |
|---------|------|----------|
| `speed` | 빠른 구조 파악 | quick |
| `precision` | 정밀 분석 | precise, critical |
| `plan_conformance` | 요구사항 준수 | cross |
| `quality_validation` | 품질 검증 | cross, critical |

### 프로토콜 캐시

| 항목 | 값 |
|------|------|
| TTL | 300초 (5분) |
| 무효화 | 파일 mtime 변경 시 |
| 저장 | 메모리 (in-process) |

---

## 7. Provider별 비용 (상대)

| Provider 유형 | 입력 단가 | 출력 단가 |
|--------------|---------|---------|
| 고성능 (Primary) | 1.0x | 1.0x |
| 중간 성능 | 0.2x | 0.3x |
| 경량 (읽기 전용) | 0.1x | 0.1x |

---

## 8. 에이전트 응답 스키마

```json
{
  "conclusion": "PASS | FAIL",
  "findings": [
    {
      "severity": "CRITICAL | HIGH | MEDIUM | LOW",
      "category": "분류명",
      "location": "대상 식별자",
      "description": "발견 내용",
      "suggestion": "권장 조치"
    }
  ],
  "evidence": [{ "id": "E1", "detail": "근거" }],
  "risks": [{ "id": "R1", "severity": "HIGH", "detail": "리스크" }],
  "action_items": [{ "id": "A1", "action": "조치 항목" }]
}
```

---

## 9. Judge 출력 스키마

```json
{
  "verdict": "PASS | FAIL",
  "rule_triggered": "rule_1 | rule_2 | rule_3 | rule_4 | rule_5",
  "reason": "판정 사유",
  "findings_summary": {
    "total": 5, "critical": 1, "high": 0, "medium": 2, "low": 2, "deduplicated": 1
  },
  "agent_conclusions": { "agent_a": "FAIL", "agent_b": "PASS" },
  "tie_break_applied": false
}
```

---

## 10. Synthesis 패턴 상세

| 패턴 | 대응 모드 | 피드백 루프 | 최대 반복 |
|------|----------|-----------|----------|
| parallel_evaluate | cross | 없음 (1회) | - |
| generate_then_validate | precise | 최대 N회 | 기본 3회 |
| implement_then_review | precise, critical | 최대 N회 | 기본 3회 |

---

## 11. 설정 구조

```json
{
  "multi_agent": {
    "default_mode": "solo",
    "auto_promotion": true,
    "auto_demotion": true,
    "max_retry": 3,
    "confidence_tie_break": false,
    "confidence_threshold": 0.3,
    "protocol_cache_ttl": 300,
    "feedback_loop_max": 3
  }
}
```
