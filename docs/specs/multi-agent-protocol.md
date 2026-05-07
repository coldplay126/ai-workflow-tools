# Multi-Agent Protocol

## 개요

기본 구현은 Claude Code(Master)가 Codex(Precision Slave)와 Claude Sonnet을 조합하는 방식입니다. 다만 프로토콜 자체는 host-agnostic 하게 설계할 수 있으며, Codex host + Claude fallback 구성도 가능합니다.

프롬프트에 `#모드` 해시태그를 붙여 사용합니다:

```
> #precise IAM 정책 변경 코드 리뷰해줘
> #cross 이 보안 설정의 영향도 분석해줘
> #critical 프로덕션 배포 계획 수립해줘
```

## 5가지 모드

| 모드 | 에이전트 | 트리거 | 타임아웃 | 용도 |
|------|---------|--------|---------|------|
| solo | Claude만 | 기본 | - | 일반 작업 |
| precise | Claude + Codex | `#precise` | 90s | 코드 분석, 보안 검토 |
| cross | Codex + Sonnet 병렬 | `#cross` | 90s | 고위험 변경, 교차 검증 |
| critical | Codex → Claude 순차 | `#critical` | 120s | 프로덕션 배포/롤백 |

## 실행 흐름

### #precise (정확도 우선)

```
사용자 → Claude(Master) → Codex(read-only 분석)
                        → Claude가 검증 + 보완
                        → 4-Block 출력
```

### #cross (교차 검증)

```
사용자 → Claude(Master) ─┬→ Codex (병렬)
                         └→ Claude Sonnet (병렬)
                        → 양쪽 결과 비교 + 차이점 하이라이트
                        → 4-Block 출력
```

### #critical (순차 심층)

```
사용자 → Claude(Master) → Step 1: Codex (정밀 분석)
                        → Step 2: Claude (Codex 결과 기반 종합 판정)
                        → 4-Block 출력
```

## 자동 승격/다운그레이드

모든 작업에 해시태그를 붙일 필요는 없습니다. Master가 위험도를 판단하여 자동 전환합니다.

### 승격 (더 신중하게)

| 전환 | 조건 |
|------|------|
| solo → cross | 보안 관련 코드 변경 (IAM, auth, security) |
| solo → critical | 프로덕션 배포/롤백, 데이터 삭제 |

### 다운그레이드 (더 효율적으로)

| 전환 | 조건 |
|------|------|
| cross → solo | Slave 타임아웃 또는 파일 구조 파악 실패 |
| precise → solo | Codex sandbox 제한으로 외부 접근 필요 |

## 4-Block Output Format

모든 멀티에이전트 결과는 이 형식을 따릅니다:

1. **결론**: 최종 답변/액션
2. **근거**: 선택 이유 + 데이터
3. **리스크**: 부작용/엣지케이스
4. **실행안**: 다음 단계

## WF 파이프라인과의 연동

활성 워크플로우 중 멀티에이전트를 사용하면:
- `.workflow/state.json`의 현재 Phase를 Slave 프롬프트에 자동 주입
- Phase별 관련 아티팩트 경로도 함께 전달
- Gate 실패 시 적절한 모드를 제안

## Codex Host 변형

같은 프로토콜을 Codex host에서 운영할 때는 다음처럼 해석합니다.

| 모드 | Codex host 해석 |
|------|-----------------|
| `#precise` | Codex local 정밀 분석, 필요 시 Claude secondary validation |
| `#cross` | Codex local 결과 + Claude CLI 결과 비교 |
| `#critical` | Codex precision pass 후 Claude synthesis 또는 fallback |

이 경우 slash command 대신 runner/script나 prompt convention을 사용합니다.

- 예: `../ai-workflow-tools/codex/run-wf.sh dispatch`
- 예: `"WF protocol: cross. Respect .workflow artifacts."`

## 사전 요구사항

- **Codex CLI 설치**: `npm install -g @openai/codex`
- **Codex MCP 등록**: `claude mcp add --scope user codex -- codex mcp-server`
- `#cross` 모드에서 Claude Sonnet 사용 시 별도 비용 발생
