## Multi-Agent Protocol (Master/Slave)

호스트 에이전트가 프롬프트의 `#` 해시태그를 인식하여 worker를 디스패치합니다.

### 모드

| 모드 | 에이전트 | 트리거 | 타임아웃 |
|------|---------|--------|---------|
| solo | 현재 호스트만 | 기본 | 실행 경로 설정 |
| precise | precision worker → primary | `#precise` | 역할별 설정 |
| cross | plan-conformance + quality-validation 병렬 | `#cross` | 역할별 설정 |
| critical | precision → quality-validation → primary | `#critical` | 역할별 설정 |

### Worker dispatch 경로 선택 (우선순위)

1. **OMP native `task`/`hub`** (현재 host가 제공할 때):
   - cmux run은 필요하지 않으며 `cmux-agent agents` 또는 `cmux-agent start`를 선행 조건으로 요구하지 않음
   - 독립적인 worker는 한 번의 batch `task`로 실행하고, 의존 작업은 앞선 결과 이후 실행
   - 실제 task ID/상태와 결과를 회수하며 `.workflow/state.json`과 gate는 parent만 변경
2. **cmux-agent broker** (native 도구가 없고 활성 run에 worker가 있을 때):
   - 조회: `cmux-agent agents --json`. run ID 생략 시 활성 run만 조회하며, 없으면 `{"run_id":null,"agents":[]}`와 exit 0을 반환
   - 후보 검사: `jq -e '.run_id != null and any(.agents[]; .role == "WORKER" and (.surface_id // "") != "")'`
   - 목록에 실제 등록된 worker 이름으로만 `cmux-agent send <worker-name> "<prompt>"` 호출. 목록은 surface/broker 생존 증명이 아니므로 전송 실패를 성공으로 간주하지 않음
   - 조회 오류는 숨기지 않고 보고하되, 선택적 cmux 경로의 부재만으로 전체 작업을 중단하거나 run을 자동 생성하지 않음
3. **MCP/provider 경로** (native 도구와 사용 가능한 cmux worker가 없을 때):
   - 제공되는 `mcp__codex__codex` 등으로 실행. 분석·리뷰 worker는 read-only
   - 사용할 실행 도구가 없으면 필요한 도구를 명시하고 중단. 실행하지 않은 결과를 만들지 않음

AWF CLI의 `.workflow/provider-config.json`에 명시된 `dispatch.surface_preference`는
이 자동 선택과 별개입니다. 명시된 surface가 unavailable/incompatible이면 실패를
보고하며 다른 surface로 조용히 우회하지 않습니다.

### 실행 규칙

**#precise** — 정확도 우선 (코드 분석, 보안 검토):
1. 위 우선순위로 read-only precision worker를 실행
2. 호스트가 worker 결과를 검증 + 보완
3. 4-Block Format으로 최종 출력

**#cross** — 교차 검증 (고위험 변경):
1. 위 우선순위로 plan-conformance와 quality-validation worker를 병렬 실행
2. 호스트가 양쪽 결과 비교 + 차이점 하이라이트
3. 4-Block Format으로 최종 출력

**#critical** — 순차 심층 분석 (프로덕션 배포, 롤백):
1. 위 우선순위로 precision worker의 코드/설정 분석 실행
2. 앞선 결과로 quality-validation worker의 영향도 검증 후 primary가 종합 판정
3. 4-Block Format: 결론/근거/리스크/실행안

### Judge Rules

`#cross`와 `#critical` 결과는 다음 순서로 판정합니다:

1. `CRITICAL` 또는 `HIGH` finding이 하나라도 있으면 **FAIL**
2. `category:location` 기준으로 중복 제거한 `MAJOR` 또는 `MEDIUM` finding이
   2건 이상이면 **FAIL**
3. PASS/FAIL 불일치에서 재현 가능하고 근거가 연결된 FAIL evidence score가
   3 이상이면 **FAIL**
4. 근거가 약하거나 재현할 수 없는 불일치, 또는 PASS와 invalid 결과의 조합은
   **ESCALATE**
5. 모든 명시적 결론이 FAIL이면 **FAIL**, 모든 유효 결론이 PASS이면 **PASS**

이 절의 Codex sandbox `read-only`는 hashtag protocol이 직접 호출하는 분석
Slave에만 적용합니다. `awf wf`가 phase provider를 실행할 때는 review/verify만
read-only이고, 쓰기가 필요한 phase는 workspace-write 정책을 사용합니다.

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
