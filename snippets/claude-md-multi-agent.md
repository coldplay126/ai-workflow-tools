## Multi-Agent Protocol (Master/Slave)

Claude Code(Master)가 프롬프트의 `#` 해시태그를 인식하여 Slave를 자동 디스패치합니다.

### 모드

| 모드 | 에이전트 | 트리거 | 타임아웃 |
|------|---------|--------|---------|
| solo | Claude만 | 기본 | - |
| precise | Claude + Codex | `#precise` | 90s |
| cross | Codex + Claude Sonnet 병렬 | `#cross` | 90s |
| critical | Codex → Claude 순차 | `#critical` | 120s |

### Slave dispatch 경로 선택 (우선순위)

Slave를 호출하기 전에 cmux-agent 활성 여부를 먼저 확인한다.

1. **cmux-agent broker** (활성 run이 있을 때 — **권장**):
   - 감지: `cmux-agent agents --json | jq -e '.agents | length > 0'`가 성공할 때만 활성으로 판정. cwd의 `.agent/control-plane.sqlite3` 존재만으로는 활성으로 판정하지 않음
   - 호출: `cmux-agent send <worker-name> "<prompt>"` 로 dispatch artifact 작성 → broker가 worker surface에 자동 주입
   - worker 이름 예: `worker-impl`, `worker-review`, `worker-verify`, `worker-investigate`
   - 장점: claude 본 세션이 다른 작업을 계속할 수 있음 (background tab에서 진행), 결과는 result artifact로 회수, 토큰 절감
2. **MCP fallback** (cmux-agent 미활성):
   - `mcp__codex__codex` (sandbox: read-only — Codex Slave 규칙 참조)
   - 단점: claude 세션 점유 + tool-call 왕복 + JSON 직렬화 오버헤드. precise/cross/critical 모드에서 응답이 수십 초 ~ 분 단위로 느려질 수 있음

운영 보고 (2026-05-13 BLIP Gem cycle §12.5): cmux-agent 활성 상태에서도 MCP를 호출하면 broker dispatch 대비 평균 3-5배 느림. 활성 검출이 명확할 때는 **반드시 broker 경로**를 사용한다.

### 실행 규칙

**#precise** — 정확도 우선 (코드 분석, 보안 검토):
1. cmux-agent 활성이면 `cmux-agent send worker-investigate "<분석 요청>"` (또는 review/verify worker), 미활성이면 `mcp__codex__codex` (sandbox: read-only)
2. Codex 결과를 Claude가 검증 + 보완 (broker 경로는 result artifact의 conclusion 필드, MCP 경로는 tool result)
3. 4-Block Format으로 최종 출력

**#cross** — 교차 검증 (고위험 변경):
1. cmux-agent 활성이면 `cmux-agent send worker-review` + `claude --print --bare --model sonnet` 병렬, 미활성이면 `mcp__codex__codex` + `claude --print --bare --model sonnet` 병렬
2. Claude Opus가 양쪽 결과 비교 + 차이점 하이라이트
3. 4-Block Format으로 최종 출력

**#critical** — 순차 심층 분석 (프로덕션 배포, 롤백):
1. Step 1 — Codex(Precision): cmux-agent broker 또는 `mcp__codex__codex`로 코드/설정 정밀 분석 (90s)
2. Step 2 — Claude(Master): Codex 결과 기반 종합 판정 + 영향도 분석
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
