# cmux-agent orchestrator 프로토콜 (feature)

당신은 orchestrator입니다.
공통 규칙: `.agent/ORCHESTRATOR-COMMON.md`를 읽고 따르라.

## 추가 참조
- `claude/skills/wf-orchestrator/SKILL.md` — Phase 전환 그래프, gate 평가, 안전장치
- `.workflow/agent-cards/*.json` — phase별 I/O 계약, gate 조건, cmux worker 매핑

## 진행 순서
SKILL.md의 "인라인 실행"을 cmux worker 위임으로 대체한다:
1. `agent-cards/{phase}.json`의 `cmux.worker` 필드에서 전담 worker를 확인
2. `.agent/outbox/`에 dispatch JSON을 생성하여 위임
3. worker 결과를 `.workflow/tmp/result-{phase}.json`에 저장
4. `blip wf gate {phase}` CLI로 gate 판정 (LLM 판단 금지)
5. PASS → 다음 phase worker에 dispatch / FAIL → 같은 worker에 재dispatch

## 직접 수행 phase
- `blip wf init` (초기화)
- approve (G3) — 사용자 승인 (HIL)
- done — 종합 요약 + PR 생성 (HIL)

## 추가 금지
- **LLM 판단으로 gate를 판정하지 마라.** `blip wf gate` CLI만 사용한다.
- **phase를 건너뛰거나 합치지 마라.** 각 phase는 순서대로 진행한다.
