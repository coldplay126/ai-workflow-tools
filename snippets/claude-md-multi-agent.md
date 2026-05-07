## Multi-Agent Protocol (Master/Slave)

Claude Code(Master)가 프롬프트의 `#` 해시태그를 인식하여 Slave를 자동 디스패치합니다.

### 모드

| 모드 | 에이전트 | 트리거 | 타임아웃 |
|------|---------|--------|---------|
| solo | Claude만 | 기본 | - |
| precise | Claude + Codex | `#precise` | 90s |
| cross | Codex + Claude Sonnet 병렬 | `#cross` | 90s |
| critical | Codex → Claude 순차 | `#critical` | 120s |

### 실행 규칙

**#precise** — 정확도 우선 (코드 분석, 보안 검토):
1. Codex(`mcp__codex__codex`, sandbox: read-only)에게 코드 분석 위임
2. Codex 결과를 Claude가 검증 + 보완
3. 4-Block Format으로 최종 출력

**#cross** — 교차 검증 (고위험 변경):
1. Codex(`mcp__codex__codex`) + Claude Sonnet(`claude --print --bare --model sonnet`) 병렬 실행
2. Claude Opus가 양쪽 결과 비교 + 차이점 하이라이트
3. 4-Block Format으로 최종 출력

**#critical** — 순차 심층 분석 (프로덕션 배포, 롤백):
1. Step 1 — Codex(Precision): 코드/설정 정밀 분석 (90s)
2. Step 2 — Claude(Master): Codex 결과 기반 종합 판정 + 영향도 분석
3. 4-Block Format: 결론/근거/리스크/실행안

### 자동 승격/다운그레이드

승격 (더 신중하게):
- `solo → cross`: 보안 관련 코드 변경 (IAM, auth, security 키워드)
- `solo → critical`: 프로덕션 배포/롤백, 데이터 삭제

다운그레이드 (더 효율적으로):
- `cross → solo`: Slave 타임아웃 또는 파일 구조 파악 실패
- `precise → solo`: Codex sandbox 제한으로 외부 접근 필요한 작업

### 4-Block Output Format

모든 멀티에이전트 결과는 이 형식을 따릅니다:
1. **결론**: 최종 답변/액션
2. **근거**: 선택 이유 + 데이터
3. **리스크**: 부작용/엣지케이스
4. **실행안**: 다음 단계

### WF 컨텍스트 인식

활성 워크플로우 중 `#precise`/`#cross`/`#critical` 실행 시:
1. `.workflow/state.json` 존재 여부 확인
2. 존재하면 Slave 프롬프트에 다음을 추가:
   - `currentPhase`, 최근 `history` 3건
   - Phase별 관련 artifact 경로:

     | Phase | 추가 artifact |
     |-------|--------------|
     | plan/review | concept.md |
     | impl | spec.md + tasks.md (미완료 항목) |
     | verify/test | allowed-files.json + impl-log.md |

   - 프롬프트 접미사: `"WF phase: {phase}. Respect .workflow/ artifacts."`
3. `.workflow/state.json`이 없으면: 주입 없이 기본 동작

### Gate 실패 시 Protocol 승격 제안

WF gate 실패 시 적절한 Protocol 모드를 **제안** (자동 실행 아님):

| 실패 유형 | 제안 모드 | 이유 |
|-----------|----------|------|
| G2 CRITICAL (review) | `#cross` | 교차 검증으로 근본 원인 |
| G4 retries ≥ 3 (impl) | `#precise` | Codex 정밀 코드 분석 |
| G5 SCOPE_VIOLATION | `#critical` | 심층 영향도 분석 |
| G5 arch_issue | `#cross` | 아키텍처 재평가 |
| G6 test retries ≥ 2 | `#precise` | 실패 테스트 정밀 분석 |
