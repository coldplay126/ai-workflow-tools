---
name: multi-agent
version: 2.2.0
description: "실질적인 기획·설계 요청을 Claude planning 역할로 위임하고 구현·교차 검증을 Claude/Codex 역할별로 실행. 서브에이전트 5모드 + 에이전트 팀."
type: protocol

capabilities:
  - multi_provider
  - parallel_execution
  - judge_rules
  - agent_team

conditions:
  trigger: "실질적인 기획·설계 산출물 작성 요청, --mode 지정, 또는 보안/프로덕션 교차 검증"
  skip: "단순 파일 읽기, 짧은 질문-답변"

subagent_modes:
  solo: { agents: 1, description: "parent only (기본)" }
  quick: { agents: 1, description: "speed(code-reviewer, Codex @task) read-only, 45s timeout" }
  precise: { agents: 2, description: "precision(code-reviewer, Codex @task) → parent 순차 검증" }
  cross: { agents: 2, description: "plan_conformance(plan-validator, Codex @task) + quality_validation(quality-validator, Claude @plan) 병렬 → Judge" }
  critical: { agents: 3, description: "precision(Codex @task) → quality_validation(Claude @plan) → parent 3단계 순차" }

team_mode:
  description: "3레이어 에이전트 팀 (Python flow → Leader mission → Worker execution)"
  execution: "sequential 또는 parallel"
  max_turns: 3
  default_teams:
    plan: "spec_writer(Claude @plan) + constitution_reviewer(Codex @task)"
    impl: "implementer(Codex @task) + code_reviewer(Codex @task, 동일 모델 독립 리뷰)"
    test: "happy_path(Codex @task) + adversarial(Claude @plan)"

judge_rules:
  - "CRITICAL/HIGH finding 하나 이상 → FAIL"
  - "category:location 중복 제거 후 MAJOR/MEDIUM 2건 이상 → FAIL"
  - "PASS/FAIL 불일치에서 유효하고 grounded인 FAIL evidence score 3 이상 → FAIL"
  - "약하거나 재현 불가한 불일치 또는 PASS+invalid 결과 → ESCALATE"
  - "모든 명시적 결론 FAIL → FAIL; 모든 유효 결론 PASS → PASS"

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

## 역할별 에이전트·모델 매핑

worker의 모델은 task 호출에서 `model`로 덮어쓰지 않는다. `awf agents sync-omp`가
source agent frontmatter의 `omp_model_role`에서 생성한 `.omp/agents/<name>.md`의
`model: "@plan"`/`"@task"` alias가 결정하며, alias가 가리키는 모델 버전은 OMP
modelRoles 설정에만 있고 repo에 하드코딩하지 않는다.

| protocol 역할 | agent (`task.agent`) | OMP model role | 담당 |
|---|---|---|---|
| spec_writer | `spec-writer` | `@plan` (Claude) | 기획·설계 산출물 작성 |
| quality_validation | `quality-validator` | `@plan` (Claude) | 품질·위험 검토 |
| wf_reviewer, review_* | `artifact-reviewer` | `@plan` (Claude) | spec↔plan↔tasks 교차 검토 |
| adversarial | `adversarial-tester` | `@plan` (Claude) | 적대적 테스트 |
| implementer | `implementer` | `@task` (Codex) | 구현 |
| precision, speed, code_reviewer | `code-reviewer` | `@task` (Codex) | 독립 코드 검토 |
| plan_conformance, constitution_reviewer | `plan-validator` | `@task` (Codex) | 계획 정합성 검토 |
| wf_verifier, spec_compliance | `spec-verifier` | `@task` (Codex) | spec 준수 검증 |
| happy_path | `happy-path-tester` | `@task` (Codex) | 정상 경로 테스트 |

이종 모델 교차 검증은 Claude 역할과 Codex 역할이 함께 참여할 때만 해당한다
(cross, critical, plan/test team). precise/quick과 impl team의
implementer + code-reviewer는 같은 모델의 독립 리뷰이며, 한 모델을 여러 개 복제한
결과를 이종 교차 검증으로 보고하지 않는다.

## 기획 위임 (OMP host parent)

OMP `@plan` 설정만으로는 현재 parent 모델이 바뀌지 않는다. parent가 Codex 등
`@plan`이 가리키는 모델이 아니면 실질적인 기획·설계 작성 — spec/plan/tasks/
test-criteria 초안, 아키텍처·설계 결정 문서화, `/wf` plan phase 산출물 — 은 parent가
직접 쓰지 않고 `task`(`agent: "spec-writer"`)로 위임한다. parent는 요구사항·컨텍스트·
제약 전달, 결과 통합, gate/state 소유만 맡는다.

- 단순 질문·설명·코드 읽기·짧은 답변, 기존 산출물의 소규모 수정에는 위임하지 않는다.
- 기획 위임은 7-phase workflow 시작이 아니다. `.workflow/state.json`이 없어도
  `awf wf init`을 자동 실행하지 않는다.
- parent의 resolved model이 이미 `@plan` 대상이면 같은 모델로 왕복하지 않고 직접 작성한다.

## 모델 확인

역할 이름이나 worker의 자기 보고가 아니라 task lifecycle event/결과의 resolved
model·provider가 위 표의 model role과 일치하는지로 확인한다. alias 미해결, provider
오류, 다른 모델로 resolve된 경우는 실패로 보고하고 중단하며 다른 모델로 조용히
대체하지 않는다.

OMP modelRoles(`@plan`/`@task`)는 OMP host의 task 런타임 설정이다. standalone
`awf wf next`의 primary provider는 별개의 provider-direct 경로로,
[wf-orchestrator](../wf-orchestrator/SKILL.md)의 "모델 결정 우선순위"를 따른다.

## OMP 호스트 실행

OMP의 `task`/`hub`가 제공되면 Python team runner의 결정론적 gate를 유지하면서
실행 계층만 host-native 기능으로 강화합니다.

OMP의 native `task`/`hub` 실행에는 cmux run이 필요하지 않습니다. 실행 전
`cmux-agent agents`를 필수 preflight로 호출하거나 `cmux-agent start`를 요구하지
않습니다. 독립적인 스킬 호출은 현재 host의 도구를 사용하고, AWF CLI 실행은
`.workflow/provider-config.json`의 명시적 dispatch 선택을 유지합니다.

cmux 경로를 사용할 때만 `cmux-agent agents --json`으로 활성 run의 worker 목록을
확인합니다. `run_id: null` 또는 사용 가능한 worker가 없는 결과는 cmux 미활성이지
멀티에이전트 전체의 실패가 아닙니다. 이 경우 사용 가능한 host-native/MCP 경로를
선택하되, `surface_preference: "cmux"`를 명시한 CLI 실행은 조용히 다른 surface로
바꾸지 않고 준비 상태 오류를 보고합니다.

1. 서로 독립적인 worker는 하나의 persisted host에서 한 번의 batch `task`로
   fan-out합니다.
2. worker별 agent type, resolved model, output schema/mode, isolation을 보존합니다.
   task에 `model`을 지정하지 않고 위 매핑 표의 agent alias에 맡기며, resolved model이
   표의 model role과 다르면 그 worker 결과를 evidence로 쓰지 않고 실패로 보고합니다.
3. capacity를 초과하면 실행 전에 실패하고, timeout/cancel 시 완료된 partial result를
   보존하면서 host와 descendant process를 reap합니다.
4. 실제 task lifecycle event의 ID와 상태만 성공 evidence로 인정하고
   `agent://`/`history://`, usage, lineage를 schema-v2 provenance로 기록합니다.
5. `awf agents followup-omp`는 exact task에 먼저 전달하며, 원 task가 unavailable일
   때만 history 기반 successor를 하나 생성합니다.
6. Judge v2는 `CRITICAL`/`HIGH`와 중복 제거된 다중 `MAJOR`/`MEDIUM`을 fail closed로
   처리하고, 약하거나 재현 불가능한 PASS/FAIL 불일치는 `ESCALATE`로 재검증합니다.
7. `.workflow/state.json`, gate, scope hash와 approve/done HIL은 parent가 계속
   단독 소유합니다.
8. Plan worker는 material choice를 canonical
   `.workflow/artifacts/planning-options.json`으로만 제안한다. `selection_required`
   일 때 worker는 `recommended_action: "user_decision"` escape를 반환하며,
   `deciding`, `awf wf select-option` journal, selected rerun과 G1 후 replan은
   parent workflow가 단독 소유한다.

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
