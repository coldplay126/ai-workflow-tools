---
name: multi-agent
version: 2.0.0
description: "멀티에이전트 교차 검증. 서브에이전트 5모드 + 에이전트 팀 3레이어 아키텍처."
type: protocol

capabilities:
  - multi_provider
  - parallel_execution
  - judge_rules
  - agent_team

conditions:
  trigger: "--mode 옵션으로 지정하거나, 보안/프로덕션 키워드 감지 시 자동 승격"
  skip: "단순 파일 읽기, 짧은 질문-답변"

subagent_modes:
  solo: { agents: 1, description: "Primary only (기본)" }
  quick: { agents: 1, description: "Codex read-only, 45s timeout" }
  precise: { agents: 2, description: "Codex → Primary 순차 검증" }
  cross: { agents: 2, description: "Codex + Sonnet 병렬 → Judge" }
  critical: { agents: 3, description: "Codex → Sonnet → Primary 3단계 순차" }

team_mode:
  description: "3레이어 에이전트 팀 (Python flow → Leader mission → Worker execution)"
  execution: "sequential 또는 parallel"
  max_turns: 3
  default_teams:
    plan: "spec_writer + constitution_reviewer"
    impl: "implementer + code_reviewer"
    test: "happy_path + adversarial"

judge_rules:
  - "어느 agent든 critical severity → FAIL"
  - "major severity 합산 2건 이상 → FAIL"
  - "agent 간 결론 불일치 → FAIL (보수적)"
  - "모든 agent PASS → PASS"

protocols:
  subagent:
    plan_conformance: "protocols/plan_conformance.md"
    quality_validation: "protocols/quality_validation.md"
    precision: "protocols/precision.md"
    speed: "protocols/speed.md"
  team_worker:
    spec_writer: "protocols/spec_writer.md"
    constitution_reviewer: "protocols/constitution_reviewer.md"
    implementer: "protocols/implementer.md"
    code_reviewer: "protocols/code_reviewer.md"
    happy_path: "protocols/happy_path.md"
    adversarial: "protocols/adversarial.md"
---

# 멀티에이전트 교차 검증

서브에이전트 5모드와 에이전트 팀 3레이어로 작업 복잡도에 따라 에이전트 조합을 자동 선택합니다.

## 실행 패턴

| 패턴 | 설정 | 사용 |
|------|------|------|
| subagent | provider-config v3 `pattern: "subagent"` | review, verify, approve, done |
| team | provider-config v3 `pattern: "team"` | plan, impl, test |

`run_phase()`가 provider-config의 `pattern` 필드로 자동 분기합니다.

## 에이전트 팀

3레이어 아키텍처:
1. **Python (Layer 1)**: 턴 순서, 종료 판정, 타임아웃 — 결정론적
2. **Leader (Layer 2)**: 미션 빌딩, 이전 턴 피드백 반영
3. **Workers (Layer 3)**: Blackboard에서 읽고 discussion에 쓰기, JSON findings 생성

워커 간 통신은 `.workflow/team/{phase}/` 디렉토리 기반 (Blackboard 패턴).

## OMP 호스트 실행

OMP의 `task`/`hub`가 제공되면 Python team runner의 결정론적 gate를 유지하면서
실행 계층만 host-native 기능으로 강화합니다.

1. 서로 독립적인 worker는 한 번의 batch `task` 호출로 fan-out합니다.
2. worker별 agent type, model/effort, output schema, isolation을 명시합니다.
3. 후속 질문이나 수정은 새 worker를 만들지 않고 `hub`로 기존 agent에 전달합니다.
4. `agent://` 결과와 `history://` transcript는 evidence/provenance로 보존합니다.
5. `.workflow/state.json`과 gate 판정은 awf가 계속 소유합니다.
6. approve/done HIL은 parent가 수행하며 subagent에 위임하지 않습니다.

순차 의존 작업, 같은 파일을 동시에 수정하는 작업, 단일 파일의 짧은 변경에는
fan-out하지 않습니다.

## 출력 형식

모든 agent 결과는 4-Block JSON:
```json
{
  "conclusion": "PASS|FAIL",
  "findings": [{"severity": "...", "description": "..."}],
  "evidence": [...],
  "risks": [...],
  "action_items": [...]
}
```

## 프로토콜 파일

각 agent의 역할과 응답 형식은 `protocols/*.md`에 정의됩니다 (spec-as-truth).
`manifest.json`에서 카테고리 선언, `spec_loader.load_skill_resource()`로 런타임 로드.
수정하면 agent 동작이 변경됩니다 (코드 수정 불필요).
