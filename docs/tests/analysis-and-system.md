# Analysis + System — Acceptance Tests

## Purpose

이 문서는 `analysis-pipeline`과 `system-overview`의 high/medium severity gap에 대한
acceptance test 기준을 정의한다.

## Scope

- 대상 gap:
  - `GAP-AS-001` ~ `GAP-AS-006`
  - `GAP-SO-001` ~ `GAP-SO-003`
  - `GAP-SO-005`
- 제외:
  - low severity gap
  - workflow / multi-agent 영역

## Dependency Notes

- `SO-S2-001`은 `AN-A6-001`, `AN-A6-002`, `AN-A2-001`의 선행 기반 테스트다.
  - mode contract와 observation contract를 외부 명세로 읽는 구조가 먼저 필요하다.
- `SO-S2-002`와 `SO-S4-001`은 함께 검증하는 편이 좋다.
  - search path 확장만 구현하고 tool-neutral root 분리를 하지 않으면 회귀가 재발하기 쉽다.
- `AN-STAGE3-001`과 `AN-A3-001`은 canonical Stage 3 semantics 확정 후 고정해야 한다.

## Test Index

| test_id | area | related_gap | priority | dependency |
|---|---|---|---|---|
| SO-S2-001 | system | GAP-SO-001 | high | - |
| SO-S2-002 | system | GAP-SO-002 | high | SO-S2-001 |
| AN-A6-001 | analysis | GAP-AS-001 | high | SO-S2-001 |
| AN-A6-002 | analysis | GAP-AS-002 | high | SO-S2-001, AN-A6-001 |
| AN-STAGE3-001 | analysis | GAP-AS-003 | medium | canonical rule 결정 필요 |
| AN-A2-001 | analysis | GAP-AS-004 | medium | SO-S2-001 |
| AN-A3-001 | analysis | GAP-AS-005 | medium | canonical rule 결정 필요 |
| AN-A5-001 | analysis | GAP-AS-006 | medium | AN-A6-002 |
| SO-S4-001 | system | GAP-SO-003 | medium | SO-S2-002 |
| SO-S3-001 | system | GAP-SO-005 | medium | - |

## Test Metadata

- Test id: `SO-S2-001`
- Area: system
- Pattern reference: `docs/patterns/system-overview/README.md` `S2`
- Related gap: `GAP-SO-001`
- Priority: `high`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - spec-as-truth는 prompt file load를 넘어 skill/spec/resource 계약 전체를 외부 명세에서 읽어야 한다.

## Preconditions

- spec root용 fixture 디렉토리가 있다.
- fixture 안에 `SKILL.md` 또는 동등한 manifest, prompt file, template/resource file이 있다.
- loader entrypoint가 spec root를 인자로 받을 수 있다.

## Test Scenario

1. fixture spec root를 준비한다.
2. analysis mode contract, prompt, resource를 각각 외부 파일로 정의한다.
3. spec loader를 호출해 manifest, prompt, resource를 함께 resolve한다.
4. 반환 결과가 prompt text뿐 아니라 mode contract/resource metadata를 포함하는지 확인한다.
5. 일부 spec 파일을 수정한 뒤 cache clear 또는 mtime/hash change 후 다시 로드한다.

## Expected Result

- loader가 prompt만이 아니라 skill/spec/resource 계약 전체를 로드한다.
- 반환 결과에 manifest metadata와 resource/template resolution 결과가 포함된다.
- spec 파일 변경 후 재로드 시 변경 내용이 반영된다.

## Failure Signal

- prompt text만 반환되고 mode contract/resource metadata가 비어 있다.
- `SKILL.md` 또는 manifest를 무시한다.
- spec 변경 후에도 stale cache 결과가 유지된다.

## Automation Plan

- 테스트 유형: `fixture + integration`
- 대상 코드/명령:
  - `cli/src/awf/core/spec_loader.py`
  - 신규 fixture runner 예: `cli/tests/run_spec_loader_fixture.py`
- 필요한 fixture:
  - spec root fixture
  - sample manifest
  - prompt/template/resource 파일 세트

## Notes

- 이 테스트가 통과해야 `analysis`의 mode contract 외부화 테스트를 안정적으로 설계할 수 있다.

## Test Metadata

- Test id: `SO-S2-002`
- Area: system
- Pattern reference: `docs/patterns/system-overview/02-architecture.md` `282-301`
- Related gap: `GAP-SO-002`
- Priority: `high`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - spec discovery는 canonical search priority를 따라 env/config/project/tool root를 일관되게 조회해야 한다.

## Preconditions

- 서로 다른 spec root 후보 디렉토리 fixture가 있다.
- 동일한 skill/spec 이름이 여러 경로에 중복 배치되어 있다.
- env 또는 config 기반 override를 테스트할 수 있다.

## Test Scenario

1. user-level, project-level, tool-level fixture root를 준비한다.
2. 동일한 spec 이름을 각 root에 서로 다른 내용으로 배치한다.
3. search priority가 정의된 상태에서 loader를 호출한다.
4. 기본 로드 시 가장 우선순위 높은 root의 spec가 선택되는지 확인한다.
5. env/config override를 켠 뒤 다시 로드한다.
6. override 적용 시 우선순위가 바뀌는지 확인한다.

## Expected Result

- loader가 canonical search order를 따른다.
- 우선순위가 높은 root가 항상 선택된다.
- env/config override가 지정되면 의도한 root가 선택된다.

## Failure Signal

- 특정 경로만 하드코딩되어 다른 root를 무시한다.
- 같은 이름의 spec가 있을 때 우선순위가 비결정적이다.
- override 설정이 있어도 결과가 바뀌지 않는다.

## Automation Plan

- 테스트 유형: `fixture + integration`
- 대상 코드/명령:
  - `cli/src/awf/core/spec_loader.py`
  - 신규 fixture runner 예: `cli/tests/run_spec_discovery_priority_fixture.py`
- 필요한 fixture:
  - 다중 spec root 디렉토리
  - 동일 이름의 skill/spec 샘플
  - env/config override 샘플

## Notes

- `SO-S4-001`과 같은 fixture root를 재사용할 수 있다.

## Test Metadata

- Test id: `AN-A6-001`
- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A6`
- Related gap: `GAP-AS-001`
- Priority: `high`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - 각 analysis mode는 고정된 required output files 집합을 갖고 completion 판정은 그 집합에 의해 결정된다.

## Preconditions

- mode contract를 외부 명세로 읽을 수 있어야 한다.
- 최소 3개 mode fixture가 있다.
  - `document`
  - `review`
  - `investigate`
- 각 mode별 required output set이 fixture 또는 spec에 정의되어 있다.

## Test Scenario

1. `document` mode fixture로 analyze를 실행한다.
2. 4개 document output이 모두 생성되면 completed로 판정되는지 확인한다.
3. `review` mode fixture로 analyze를 실행하고 `review-report.md`만 제공한다.
4. `review` mode가 document 4파일을 요구하지 않고 review contract로 completed 되는지 확인한다.
5. `investigate` mode도 동일하게 mode별 output contract로 판정되는지 확인한다.
6. 각 mode에서 required output 하나를 누락한 상태로 재실행한다.

## Expected Result

- mode마다 서로 다른 required output set이 적용된다.
- mode에 맞는 output set이 충족되면 completed가 된다.
- mode-specific required output이 누락되면 failure 또는 incomplete로 남는다.

## Failure Signal

- 어떤 mode에서도 document 4파일을 공통으로 요구한다.
- mode-specific output 누락인데도 completed가 된다.
- mode contract를 읽지 못해 default set으로 fallback 한다.

## Automation Plan

- 테스트 유형: `fixture + e2e`
- 대상 코드/명령:
  - `cli/src/awf/core/analysis_outputs.py`
  - `cli/src/awf/commands/analyze.py`
  - 신규 fixture runner 예: `cli/tests/run_analysis_mode_contract_fixture.py`
- 필요한 fixture:
  - mode별 spec fixture
  - mode별 expected output fixture
  - fixture provider 응답 세트

## Notes

- `SO-S2-001` 미통과 상태에서는 이 테스트를 provisional로만 유지한다.

## Test Metadata

- Test id: `AN-A6-002`
- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A6`
- Related gap: `GAP-AS-002`
- Priority: `high`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - mode에 따라 Writer 집합과 prompt contract가 함께 결정되어야 한다.

## Preconditions

- mode contract가 외부 명세로 정의되어 있다.
- Writer별 fixture provider 응답을 분기할 수 있다.
- Stage 2 fanout 결과를 관찰할 수 있다.

## Test Scenario

1. `document` mode에서 analyze를 실행한다.
2. Stage 2가 `structure`, `behavior` Writer만 호출하는지 확인한다.
3. `review` mode에서 analyze를 실행한다.
4. Stage 2가 `security`, `quality`, `performance` 등 review용 Writer 집합을 호출하는지 확인한다.
5. `investigate` mode에서 별도의 Writer 집합이 선택되는지 확인한다.
6. 각 mode에서 정의되지 않은 Writer가 호출되지 않는지 확인한다.

## Expected Result

- mode별 Writer 집합이 다르게 선택된다.
- 호출된 Writer와 생성된 output contract가 일치한다.
- mode에 없는 Writer는 호출되지 않는다.

## Failure Signal

- 모든 mode에서 `structure`, `behavior`만 고정 호출된다.
- mode별 output은 바뀌었지만 Writer 집합은 바뀌지 않는다.
- 정의되지 않은 Writer가 호출되어 contract와 불일치가 발생한다.

## Automation Plan

- 테스트 유형: `fixture + integration`
- 대상 코드/명령:
  - `cli/src/awf/core/analysis_fanout.py`
  - `cli/src/awf/core/analysis_prompt.py`
  - 기존 fixture 확장 예: `cli/tests/run_analysis_fanout_fixture.py`
- 필요한 fixture:
  - mode별 Writer spec
  - Writer별 fixture responses
  - Stage 2 call trace capture

## Notes

- `AN-A6-001`과 같은 fixture root를 재사용하는 편이 좋다.

## Test Metadata

- Test id: `AN-STAGE3-001`
- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/02-stages.md` `83-87`
- Related gap: `GAP-AS-003`
- Priority: `medium`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - Stage 3 진입 조건은 canonical rule 하나로 판정되어야 한다.

## Preconditions

- Stage 3 canonical rule이 pattern 수정 또는 구현 수정으로 확정되어 있어야 한다.
- `deep`, `stage_routing`, `related_domains`, `stage3_force`를 제어할 수 있는 fixture가 있다.

## Test Scenario

1. canonical rule에 맞춘 truth table을 정의한다.
2. 각 조합에 대해 analyze를 실행한다.
3. `stage3.status`가 `skipped`, `scaffold`, `completed` 중 무엇으로 기록되는지 확인한다.
4. canonical rule이 false인 조합에서는 Stage 3 provider run이 없는지 확인한다.
5. canonical rule이 true인 조합에서는 Stage 3 artifact가 생성되는지 확인한다.

## Expected Result

- 모든 조합이 truth table과 동일한 판정을 낸다.
- Stage 3는 canonical rule을 벗어난 암묵적 승격/강등이 없다.

## Failure Signal

- 문서 truth table과 다른 조합에서 Stage 3가 켜지거나 꺼진다.
- `related_domains` 또는 `stage3_force`가 문서와 다르게 작동한다.

## Automation Plan

- 테스트 유형: `fixture + integration`
- 대상 코드/명령:
  - `cli/src/awf/commands/analyze.py`
  - `cli/src/awf/core/analysis_state.py`
  - 신규 fixture runner 예: `cli/tests/run_analysis_stage3_matrix_fixture.py`
- 필요한 fixture:
  - Stage 3 condition matrix fixture
  - fake provider responses

## Notes

- canonical rule이 고정되기 전에는 expected matrix를 잠그지 말아야 한다.

## Test Metadata

- Test id: `AN-A2-001`
- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A2`
- Related gap: `GAP-AS-004`
- Priority: `medium`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - Stage 1 artifact는 observation만 포함하고 judgment/compat translation은 별도 계층에 존재해야 한다.

## Preconditions

- Stage 1 output artifact를 직접 읽을 수 있다.
- fixture input 코드가 있다.
- compat 필드와 observation payload를 구분할 수 있다.

## Test Scenario

1. fixture repo에 대해 Stage 1만 포함하는 analyze run을 수행한다.
2. 생성된 Stage 1 artifact JSON을 읽는다.
3. observation payload에 사실 계층 필드만 있는지 확인한다.
4. severity, conclusion, recommendation 또는 v2 compat 전용 필드가 Stage 1 artifact에 없는지 확인한다.
5. migration adapter가 있다면 별도 artifact 또는 변환 단계에서만 compat field가 생기는지 확인한다.

## Expected Result

- Stage 1 artifact는 observation-only 구조를 가진다.
- compat 또는 judgment 필드는 Stage 1 원본 artifact에 존재하지 않는다.

## Failure Signal

- Stage 1 artifact에 severity/recommendation/conclusion이 들어 있다.
- v2 compat 필드가 원본 observation payload에 섞여 있다.

## Automation Plan

- 테스트 유형: `integration + fixture`
- 대상 코드/명령:
  - `cli/src/awf/core/analysis_stage1.py`
  - `cli/src/awf/core/analysis_writer.py`
  - 기존 fixture 확장 예: `cli/tests/run_analysis_fixture.py`
- 필요한 fixture:
  - Stage 1 only input fixture
  - expected observation schema fixture

## Notes

- `SO-S2-001`이 구현되면 observation schema도 외부 spec에서 읽도록 확장할 수 있다.

## Test Metadata

- Test id: `AN-A3-001`
- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A3`
- Related gap: `GAP-AS-005`
- Priority: `medium`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - 실패 시 마지막 완료 stage 이후부터 재개하며, Stage 3도 동일한 재개 contract를 따라야 한다.

## Preconditions

- Stage 3 canonical state model이 확정되어 있어야 한다.
- Stage 3 직전과 Stage 3 도중 실패를 인위적으로 만들 수 있는 fixture가 있다.

## Test Scenario

1. analyze를 실행해 Stage 2 completed 상태까지 만든다.
2. Stage 3 scaffold 또는 live run 도중 실패를 발생시킨다.
3. state file과 artifact를 저장한다.
4. 동일 입력으로 재실행한다.
5. Stage 1/2를 재실행하지 않고 Stage 3부터 재개하는지 확인한다.
6. Stage 3 completed 후 다시 재실행하여 no-op 또는 skip이 되는지 확인한다.

## Expected Result

- 실패 후 재실행 시 마지막 completed stage 이전은 재사용된다.
- Stage 3도 동일한 resume contract를 가진다.
- 이미 completed된 Stage 3는 중복 실행되지 않는다.

## Failure Signal

- Stage 3 실패 후 재실행 시 Stage 1 또는 Stage 2까지 다시 돈다.
- Stage 3 completion marker가 있어도 다시 실행된다.

## Automation Plan

- 테스트 유형: `fixture + e2e`
- 대상 코드/명령:
  - `cli/src/awf/core/analysis_resume.py`
  - `cli/src/awf/core/analysis_store.py`
  - `cli/src/awf/commands/analyze.py`
  - 신규 fixture runner 예: `cli/tests/run_analysis_resume_stage3_fixture.py`
- 필요한 fixture:
  - failure injection fixture
  - persisted state/artifact fixture

## Notes

- `AN-STAGE3-001`과 함께 truth table과 state machine을 맞춰야 한다.

## Test Metadata

- Test id: `AN-A5-001`
- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A5`
- Related gap: `GAP-AS-006`
- Priority: `medium`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - Analysis Judge는 Writer evidence를 재해석할 수 있어도 원본 evidence를 변조하지 못한다.

## Preconditions

- Writer result fixture와 Judge result fixture를 주입할 수 있다.
- Judge parser/validator가 테스트 대상 함수로 분리되어 있거나 호출 가능해야 한다.

## Test Scenario

1. 고정된 Writer claims/evidence fixture를 준비한다.
2. 동일 evidence를 참조하는 정상 Judge output을 validator에 통과시킨다.
3. evidence wording 또는 source span을 수정한 Judge output을 주입한다.
4. evidence는 같고 verdict만 바꾼 Judge output도 주입한다.
5. validator가 evidence mutation만 실패로 처리하는지 확인한다.

## Expected Result

- Judge가 evidence를 그대로 참조한 경우 통과한다.
- evidence text, source span, identity가 바뀐 경우 실패한다.
- evidence는 유지되고 verdict/merge만 바뀐 경우는 contract에 맞게 허용된다.

## Failure Signal

- Judge가 evidence를 바꿔도 통과한다.
- evidence가 유지됐는데도 정상 결과를 과도하게 실패 처리한다.

## Automation Plan

- 테스트 유형: `unit + fixture`
- 대상 코드/명령:
  - `cli/src/awf/core/analysis_writer.py`
  - `cli/src/awf/core/analysis_fanout.py`
  - 기존 단위 테스트 확장: `cli/tests/test_analysis_writer.py`
- 필요한 fixture:
  - WriterResult fixture
  - JudgeResult fixture
  - mutated evidence fixture

## Notes

- mode별 Writer 구성이 도입되더라도 evidence immutability 검사는 공통 validator로 유지하는 편이 좋다.

## Test Metadata

- Test id: `SO-S4-001`
- Area: system
- Pattern reference: `docs/patterns/system-overview/README.md` `S4`
- Related gap: `GAP-SO-003`
- Priority: `medium`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - 동일한 spec는 Claude 전용 디렉토리 구조가 아니라 tool-agnostic spec root에서 동작해야 한다.

## Preconditions

- tool-neutral spec root fixture가 있다.
- Claude-style layout이 아닌 일반 spec root를 loader가 받을 수 있다.
- 최소 2개 provider/tool 조합이 있다.

## Test Scenario

1. Claude 전용 경로가 아닌 일반 spec root를 준비한다.
2. 동일 spec로 `fixture` provider 또는 `codex`/`openai` fixture path를 실행한다.
3. tool adapter만 바꿔 같은 spec가 로드되는지 확인한다.
4. 기존 `claude/skills` 경로가 없어도 실행이 가능한지 확인한다.

## Expected Result

- spec root가 tool-neutral 경로여도 실행이 된다.
- tool/provider를 바꿔도 같은 spec contract가 유지된다.
- `claude/skills` 경로 의존이 없어도 로딩이 가능하다.

## Failure Signal

- `claude/skills`가 없으면 로딩이 실패한다.
- tool/provider 변경 시 spec resolution 결과가 달라진다.

## Automation Plan

- 테스트 유형: `fixture + e2e`
- 대상 코드/명령:
  - `cli/src/awf/core/spec_loader.py`
  - provider selection entrypoint
  - 신규 fixture runner 예: `cli/tests/run_tool_neutral_spec_fixture.py`
- 필요한 fixture:
  - tool-neutral spec root
  - multi-provider execution fixture

## Notes

- `SO-S2-002`와 동일한 spec discovery fixture를 공유할 수 있다.

## Test Metadata

- Test id: `SO-S3-001`
- Area: system
- Pattern reference: `docs/patterns/system-overview/02-architecture.md` `391-549`
- Related gap: `GAP-SO-005`
- Priority: `medium`

## Requirement

- 검증하려는 invariant 또는 derived rule:
  - event system은 단순 emit를 넘어서 persistence, replay, subscription을 제공하거나 이를 명시적으로 비목표로 정의해야 한다.

## Preconditions

- canonical event platform scope가 합의되어 있어야 한다.
- event sink fixture와 long-running task fixture가 있다.

## Test Scenario

1. long-running task를 event wrapper로 실행한다.
2. emitted event를 in-memory sink와 persisted sink에 동시에 기록한다.
3. 실행 종료 후 persisted event log를 다시 읽어 sequence를 재구성한다.
4. subscription API 또는 replay helper가 있다면 저장된 log에서 재생한다.
5. 만약 persistence/replay를 비목표로 축소했다면, 그 축소된 contract가 reference/status에 명시되었는지 확인한다.

## Expected Result

- event platform scope가 테스트 가능한 contract로 고정된다.
- persistence/replay를 목표로 했다면 event log 재구성이 가능하다.
- 비목표로 축소했다면 문서와 구현이 같은 축소 범위를 가진다.

## Failure Signal

- event가 메모리에서만 사라져 재구성할 수 없다.
- 구현 범위와 문서 범위가 서로 다르다.
- replay/subscription contract가 없는데도 문서에 플랫폼처럼 서술된다.

## Automation Plan

- 테스트 유형: `integration + fixture`
- 대상 코드/명령:
  - `cli/src/awf/core/events.py`
  - `cli/src/awf/core/event_processor.py`
  - 기존 fixture 확장 예: `cli/tests/run_gateway_event_fixture.py`
- 필요한 fixture:
  - persisted event sink fixture
  - long-running task fixture
  - replay/subscription fixture 또는 contract assertion helper

## Notes

- 이 테스트는 구현 확장 또는 pattern 축소 중 어느 방향을 택하더라도 필요하다.
