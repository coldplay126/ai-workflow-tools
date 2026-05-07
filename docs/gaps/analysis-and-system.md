# Analysis + System — Gap Inventory

## Purpose

이 문서는 `analysis-pipeline`과 `system-overview`의 `pattern` 대비 현재 구현 차이를
작업 가능한 backlog로 통합 관리한다.

## Scope

- Analysis source:
  - `docs/status/analysis-pipeline.md`
- System source:
  - `docs/status/system-overview.md`
- 관련 pattern:
  - `docs/patterns/analysis-pipeline/`
  - `docs/patterns/system-overview/`

## Gap Table

| id | area | pattern_ref | status_ref | summary | severity | state | owner | test_id | resolution |
|---|---|---|---|---|---|---|---|---|---|
| GAP-AS-001 | analysis | `analysis-pipeline/README.md:A6` | `status/analysis-pipeline.md:143` | mode별 output files/Writer/Judge가 mode contract에서 동적 로딩되며, output write/completion 판정/report 생성이 mode-aware하게 전환되었다 | high | fixed | unassigned | AN-A6-001 | `c91013e`→Step 3-5: output write, report generation, Judge prompt, completeness check 모두 mode contract 기반으로 전환 완료 |
| GAP-AS-002 | analysis | `analysis-pipeline/README.md:A6` | `status/analysis-pipeline.md:144` | mode별 Writer 구성이 mode contract에서 동적으로 로딩된다. document mode 외 Writer는 contract 파일 추가로 확장 가능 | high | fixed | unassigned | AN-A6-002 | Step 3: get_writer_configs(mode)로 Writer registry 동적 로딩, run_stage2_fanout에서 mode contract 기반 Writer 실행 |
| GAP-AS-003 | analysis | `analysis-pipeline/02-stages.md:83-87` | `status/analysis-pipeline.md:145` | Stage 3 trigger 규칙을 pattern에 명시하여 코드와 정합 | medium | fixed | unassigned | AN-STAGE3-001 | pattern/reference에 related_domains>=3 auto-enable + stage3_force 규칙 codify |
| GAP-AS-004 | analysis | `analysis-pipeline/README.md:A2` | `status/analysis-pipeline.md:146` | Stage 1 artifact에서 v2 compat 필드를 제거하고 소비 시점 on-demand 파생으로 전환 | medium | fixed | unassigned | AN-A2-001 | parse_observation에서 compat 제거, _derive_summary + fallback on-demand 파생 |
| GAP-AS-005 | analysis | `analysis-pipeline/README.md:A3` | `status/analysis-pipeline.md:147` | Stage 3 resume가 Stage 2와 동일한 retry/cleanup contract로 통일됨 | medium | fixed | unassigned | AN-A3-001 | resolve_analysis_resume()에 Stage 3 retry 로직 추가, analyze.py에서 stage3_retry_blocked 가드 |
| GAP-AS-006 | analysis | `analysis-pipeline/README.md:A5` | `status/analysis-pipeline.md:148` | Judge output의 evidence/source_files 불변을 런타임에서 검증한다 | medium | fixed | unassigned | AN-A5-001 | validate_evidence_integrity() 추가, run_stage2_fanout에서 호출, metadata에 violations 기록 |
| GAP-AS-007 | analysis | `analysis-pipeline/01-overview.md:108-127` | `status/analysis-pipeline.md:149` | 코드에서 `analysis_mode`(output mode)로 명확히 분리. CLI에 별도 execution mode 개념 없음 | low | fixed | unassigned | AN-GLOSSARY-001 | `analysis_mode` 필드명으로 output mode를 명시. CLI에 execution_mode 미존재로 충돌 없음 |
| GAP-SO-001 | system | `system-overview/README.md:S2` | `status/system-overview.md:131` | spec-as-truth 범용화 완료. manifest 스키마 + load_skill_resource 범용 API + 파이프라인 통합 (analysis/workflow/multi-agent) | high | fixed | unassigned | SO-S2-001 | `66d5852` manifest.json + load_skill_resource + list_skill_resources + SkillManifest + 폴백상수 제거 + team_runner/multi_agent spec_loader 경유 |
| GAP-SO-002 | system | `system-overview/02-architecture.md:282-301` | `status/system-overview.md:132` | skill search path를 6경로로 확장. `skills.find_skill_dir()`이 단일 resolver | high | fixed | unassigned | SO-S2-002 | `skill_search_paths()`에 `~/.claude/skills`, `{repo}/claude/skills` 추가. `spec_loader`/`multi_agent`가 통합 resolver 사용 |
| GAP-SO-003 | system | `system-overview/README.md:S4` | `status/system-overview.md:133` | tool neutrality: `spec_loader`/`multi_agent`의 하드코딩 제거, `skills.find_skill_dir()` 위임 | medium | fixed | unassigned | SO-S4-001 | `spec_loader._find_skill_dir()` 제거 → `skills.find_skill_dir()` 위임. `multi_agent._load_protocol()` 하드코딩 → 동일 resolver. tool-agnostic 경로(`.awf/skills`, `~/.config/awf/skills`) 우선 |
| GAP-SO-004 | system | `system-overview/README.md:S5` | `status/system-overview.md:134` | provider alias가 config 기반으로 동작하며 permission과 통합됨 | medium | fixed | unassigned | SO-S5-001 | registry._alias() config 로딩 + provider_permission_name() alias 통합 |
| GAP-SO-005 | system | `system-overview/02-architecture.md:73-81` | `status/system-overview.md:135` | pattern과 implementation이 정합. gap의 원래 참조(391-549)는 현재 pattern에 존재하지 않음 (stale reference). pattern/reference 모두 lightweight emitter만 요구 | medium | fixed | unassigned | SO-S3-001 | stale gap reference 해소. pattern(73-81)은 observability-only emitter를 요구하며 persistence/subscription을 요구하지 않음 |
| GAP-SO-006 | system | `system-overview/02-architecture.md:603-649` | `status/system-overview.md:136` | permission model에 fnmatch 기반 wildcard 매칭 추가. disabled 우선 규칙 유지 | medium | fixed | unassigned | SO-PERM-001 | _matches_any() + fnmatch. provider:*, tool:file.* 등 지원. 하위 호환 |
| GAP-SO-007 | system | `system-overview/02-architecture.md:105-138` | `status/system-overview.md:137` | config 경로와 키 이름이 pattern 예시와 다르다. 실제: `.awf.toml` + `~/.config/awf/config.toml` | low | fixed | unassigned | SO-CONFIG-001 | 실제 구현 경로(`.awf.toml`, `~/.config/awf/config.toml`)를 canonical로 확정. pattern 예시는 reference로 축소 |

## Gap Entries

### GAP-AS-001 — Mode-Specific Output Contract ✅ Fixed

- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A6`
- Status reference: `docs/status/analysis-pipeline.md` `AP-STATUS-001`
- Summary: mode별 output files, Writer, Judge가 mode contract(`document.json`)에서 동적 로딩되며, output write/completion 판정/report 생성이 mode-aware하게 전환되었다.
- Why it matters: mode별 산출물 계약이 없으면 `document / review / investigate`를 설계상 독립 mode로 취급할 수 없다.
- Expected behavior: 각 mode는 고정된 required output files 세트를 가지며 completion 판정도 그 세트에 의해 결정된다.
- Current behavior: `get_required_output_files(mode)`가 mode contract에서 동적 로딩. `write_stage2_outputs()`, `generate_analysis_report()`, `analyze_stage2_output()`, `output_files_present()` 모두 `context.analysis_mode` 기반으로 전환 완료.
- Severity: high
- State: fixed
- Owner: unassigned
- Test id: `AN-A6-001`
- Resolution: `c91013e`에서 mode별 output registry와 accessor 추가. Step 3-5에서 analysis_outputs/fanout/store/writer의 모든 소비자를 mode contract 기반으로 전환.
- Cross references:
  - `GAP-AS-002`
  - `GAP-SO-001`

### GAP-AS-002 — Mode-Specific Writer Set ✅ Fixed

- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A6`
- Status reference: `docs/status/analysis-pipeline.md` `AP-STATUS-002`
- Summary: mode별 Writer 구성이 mode contract에서 동적으로 로딩된다. `run_stage2_fanout()`이 `get_writer_configs(mode)`로 Writer 목록, prompt, 병렬 실행 수를 결정한다.
- Why it matters: output contract만 분기해도 생성 주체가 고정이면 mode 특화 분석이 불가능하다.
- Expected behavior: mode에 따라 Writer 집합과 prompt contract가 함께 결정된다.
- Current behavior: `get_writer_configs(analysis_mode)`가 mode contract에서 Writer 목록 로딩. `run_stage2_fanout()`의 metadata, prompt 생성, executor, completeness check 모두 동적 writer_configs 사용.
- Severity: high
- State: fixed
- Owner: unassigned
- Test id: `AN-A6-002`
- Resolution: Step 3에서 `run_stage2_fanout()` 전면 전환. `get_writer_configs(mode)` → writer_configs → build_writer_prompts/ThreadPoolExecutor/completeness check 연결.
- Cross references:
  - `GAP-AS-001`
  - `GAP-SO-001`

### GAP-AS-003 — Stage 3 Trigger ✅ Fixed

- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/02-stages.md` `83-87`
- Status reference: `docs/status/analysis-pipeline.md` `AP-STATUS-003`
- Summary: Stage 3 trigger 규칙(우선순위 테이블)을 pattern에 명시하여 코드와 정합 달성.
- Why it matters: pattern을 기준으로 테스트를 설계하면 구현과 문서가 계속 어긋난다.
- Expected behavior: Stage 3 조건이 pattern과 구현에서 동일한 규칙으로 판정된다.
- Current behavior: pattern과 코드가 동일한 3단계 우선순위 규칙을 따른다: `stage3_force` > `related_domains >= 3` auto-enable > scale routing 기본 정책.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `AN-STAGE3-001`
- Resolution: 현재 코드 규칙을 canonical rule로 승격. pattern(02-stages.md §Stage 3 실행 조건)에 우선순위 테이블 추가, reference(analysis-pipeline.md §2)에 auto-enable 각주 추가.

### GAP-AS-004 — Observation and Compat Fields ✅ Fixed

- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A2`
- Status reference: `docs/status/analysis-pipeline.md` `AP-STATUS-004`
- Summary: Stage 1 artifact에서 v2 compat 필드를 제거. 소비 시점에서 observation.json으로부터 on-demand 파생.
- Why it matters: observation 계층이 judgment 언어 또는 legacy 표현을 품으면 downstream contract가 흐려진다.
- Expected behavior: Stage 1 artifact는 observation만 포함하고 judgment/compat translation은 별도 계층에 격리된다.
- Current behavior: `parse_observation()`이 `path/role/language/lines/observation`만 반환. summary는 `_derive_summary()`로, imports/compat은 fallback 경로에서 observation.json으로부터 on-demand 생성.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `AN-A2-001`
- Resolution: parse_observation()에서 compat 필드(summary, exports, imports, dependencies, complexity) 제거. analysis_prompt.py에 _derive_summary() 추가. format_file_analyses_for_memo() fallback에서 observation.json 기반 on-demand 파생.
- Cross references:
  - `GAP-SO-001`

### GAP-AS-005 — Stage 3 Resume Contract Not Unified ✅ Fixed

- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A3`
- Status reference: `docs/status/analysis-pipeline.md` `AP-STATUS-005`
- Summary: Stage 3 resume가 Stage 2와 동일한 retry/cleanup contract로 통일됨.
- Why it matters: 실패 후 재개 contract가 stage별로 다르면 운영자가 재시도 범위를 예측하기 어렵다.
- Expected behavior: 모든 stage가 동일한 completion/resume 기준을 가진다.
- Current behavior: `resolve_analysis_resume()`에 Stage 3 retry 로직 추가, `analyze.py`에서 `stage3_retry_blocked` 가드.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `AN-A3-001`
- Resolution: `resolve_analysis_resume()`에 Stage 3 retry 로직 추가, `analyze.py`에서 `stage3_retry_blocked` 가드.

### GAP-AS-006 — Analysis Judge Evidence Immutability ✅ Fixed

- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/README.md` `A5`
- Status reference: `docs/status/analysis-pipeline.md` `AP-STATUS-006`
- Summary: `validate_evidence_integrity()`가 Judge output의 evidence/source_files 불변을 런타임에서 검증한다.
- Why it matters: prompt 준수에만 의존하면 provider나 prompt drift 시 Judge가 evidence를 재작성할 수 있다.
- Expected behavior: Judge는 Writer evidence를 재해석할 수 있어도 원본 evidence를 변조하지 못해야 한다.
- Current behavior: `run_stage2_fanout()` Phase 4b에서 Writer claims와 Judge merged_claims의 evidence/source_files를 비교. 위반 시 metadata에 기록 + stderr 경고.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `AN-A5-001`
- Resolution: `validate_evidence_integrity()` in analysis_writer.py. 3가지 위반 유형 탐지: evidence_modified, source_files_modified, unknown_claim_id.

### GAP-AS-007 — Mode Terminology ✅ Fixed

- Area: analysis
- Pattern reference: `docs/patterns/analysis-pipeline/01-overview.md` `108-127`
- Status reference: `docs/status/analysis-pipeline.md` `AP-STATUS-007`
- Summary: 코드에서 `analysis_mode` (output mode: document/review/investigate)로 명확히 분리됨. CLI에 별도 execution mode 개념이 없어 충돌 해소.
- Severity: low
- State: fixed
- Owner: unassigned
- Test id: `AN-GLOSSARY-001`
- Resolution: `AnalysisContext.analysis_mode` 필드명으로 output mode를 명시적으로 분리. CLI에 execution_mode 파라미터가 존재하지 않으므로 실질적 충돌 없음.
- Cross references:
  - `GAP-SO-001` (fixed)

### GAP-SO-001 — Spec-As-Truth: Generic Spec System ✅ Fixed

- Area: system
- Pattern reference: `docs/patterns/system-overview/README.md` `S2`
- Status reference: `docs/status/system-overview.md` `SO-STATUS-001`
- Summary: spec-as-truth 범용화 완료. manifest 스키마 + load_skill_resource 범용 API + 파이프라인 통합.
- Why it matters: spec-as-truth가 prompt include 수준에 머물면 pipeline별 contract를 일관되게 외부 명세화하기 어렵다.
- Expected behavior: prompt, mode contract, skill metadata, resource/template discovery를 하나의 spec system으로 읽는다.
- Current behavior: `spec_loader.py`가 manifest 기반 범용 리소스 로딩(`load_skill_resource`, `list_skill_resources`, `load_manifest`) 지원. analysis/workflow/multi-agent 파이프라인 전체가 spec_loader 경유. SkillManifest로 런타임 디스커버리 연결.
- Severity: high
- State: fixed
- Owner: unassigned
- Test id: `SO-S2-001`
- Resolution: `66d5852` manifest.json + load_skill_resource + list_skill_resources + SkillManifest + 폴백상수 제거 + team_runner/multi_agent spec_loader 경유.
- Cross references:
  - `GAP-AS-001` (fixed)
  - `GAP-AS-002` (fixed)
  - `GAP-AS-004` (fixed)
  - `GAP-AS-007` (fixed)

### GAP-SO-002 — Skill Search Priority Narrower Than Pattern ✅ Fixed

- Area: system
- Pattern reference: `docs/patterns/system-overview/02-architecture.md` `282-301`
- Status reference: `docs/status/system-overview.md` `SO-STATUS-002`
- Summary: skill search path를 6경로로 확장하고 `skills.find_skill_dir()`을 단일 resolver로 통합.
- Why it matters: spec discovery 범위가 좁으면 tool neutrality와 spec-as-truth가 환경에 따라 깨진다.
- Expected behavior: pattern에 정의된 search priority 또는 축소된 canonical priority가 일관되게 적용된다.
- Current behavior: `skill_search_paths()`가 6경로를 dedup 포함 반환: `AWF_SKILLS_DIR` → `~/.config/awf/skills` → `{repo}/.awf/skills` → `~/.claude/skills` → `{repo}/claude/skills` → `{repo}/.claude/skills`. `spec_loader`와 `multi_agent`가 모두 `skills.find_skill_dir()`을 사용.
- Severity: high
- State: fixed
- Owner: unassigned
- Test id: `SO-S2-002`
- Resolution: `skills.py`에 `find_skill_dir()` + `_fallback_roots()` 추가. `spec_loader.py`에서 `_SEARCH_PATHS`/`_find_skill_dir()` 제거하고 import 전환. `multi_agent.py`에서 하드코딩 경로를 `find_skill_dir("multi-agent")` 호출로 교체.
- Cross references:
  - `GAP-SO-003` (fixed)

### GAP-SO-003 — Tool Neutrality Still Coupled to Claude Layout ✅ Fixed

- Area: system
- Pattern reference: `docs/patterns/system-overview/README.md` `S4`
- Status reference: `docs/status/system-overview.md` `SO-STATUS-003`
- Summary: `spec_loader`/`multi_agent`의 claude 경로 하드코딩을 제거하고, `skills.find_skill_dir()` 단일 resolver로 위임. tool-agnostic 경로(`.awf/skills`, `~/.config/awf/skills`)가 claude 경로보다 우선.
- Why it matters: 같은 spec가 어떤 AI 도구에서든 동작해야 한다는 상위 원칙과 충돌한다.
- Expected behavior: spec root와 tool adapter가 분리되어 Claude 전용 디렉토리 구조가 필수가 아니어야 한다.
- Current behavior: `skills.find_skill_dir()`이 단일 resolver. `spec_loader.py`는 자체 `_SEARCH_PATHS` 제거, `multi_agent.py`는 하드코딩 제거. 경로 우선순위: awf paths(1-3) > claude paths(4-6). claude 경로는 legacy 호환용으로만 유지.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `SO-S4-001`
- Resolution: `spec_loader._find_skill_dir()` 삭제 → `from awf.core.skills import find_skill_dir`. `multi_agent._load_protocol()` 내 하드코딩 → `find_skill_dir("multi-agent")`. fallback roots로 repo root 미탐지 시에도 동작.
- Cross references:
  - `GAP-SO-002` (fixed)

### GAP-SO-004 — Provider Pluggability Policy Is Thin ✅ Fixed

- Area: system
- Pattern reference: `docs/patterns/system-overview/README.md` `S5`
- Status reference: `docs/status/system-overview.md` `SO-STATUS-004`
- Summary: provider alias가 config 기반으로 동작하며 permission과 통합됨.
- Why it matters: 동적 모델 선택이 가능해도 실제 라우팅/확장 정책이 빈약하면 S5 달성 범위가 좁다.
- Expected behavior: alias, custom provider discovery, capability-based routing policy가 지원된다.
- Current behavior: `registry._alias()` config 로딩 + `provider_permission_name()` alias 통합.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `SO-S5-001`
- Resolution: `registry._alias()` config 로딩 + `provider_permission_name()` alias 통합.

### GAP-SO-005 — Event Platform Is Lightweight Only ✅ Fixed (Stale Reference)

- Area: system
- Pattern reference: `docs/patterns/system-overview/02-architecture.md` `73-81` (원래 gap은 391-549를 참조했으나 현재 pattern 파일은 116줄)
- Status reference: `docs/status/system-overview.md` `SO-STATUS-005`
- Summary: pattern과 implementation이 이미 정합. gap의 원래 참조는 stale.
- Why it matters: gap 참조가 stale이면 존재하지 않는 목표를 향해 구현을 진행하게 된다.
- Expected behavior (pattern 기준): "이벤트는 관측성을 위한 것이며, 제어 흐름에 영향을 주지 않는다. EventProcessor가 이벤트를 수집하고 핸들러에 전달한다." (02-architecture.md:77-79)
- Current behavior: 20/21 EventType 활성 사용. 4개 handler (ProgressDisplay, ArtifactManager, AnalysisStateUpdater, WorkflowStateUpdater)가 관측성/메타데이터/상태동기 커버. pattern이 요구하는 "observability-only emitter + handler 전달" 구조를 충족.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `SO-S3-001`
- Resolution: gap의 원래 pattern reference(391-549)가 현재 문서에 존재하지 않음 확인. 현재 pattern(73-81)과 reference(71-93) 모두 persistence/subscription/replay를 요구하지 않으며, 현행 구현이 pattern 수준을 충족. MULTI_AGENT_STARTED 데드코드는 별도 cleanup (low priority).
- Cross references:
  - reference 문서 `docs/reference/system-overview.md:71-93`: EventType 카테고리/heartbeat만 정의, persistence 미언급

### GAP-SO-006 — Permission Policy Language Too Narrow ✅ Fixed

- Area: system
- Pattern reference: `docs/patterns/system-overview/02-architecture.md` `603-649`
- Status reference: `docs/status/system-overview.md` `SO-STATUS-006`
- Summary: permission model에 fnmatch 기반 wildcard 매칭 추가. disabled 우선 규칙 유지.
- Why it matters: provider/tool 조합이 늘수록 exact string 정책만으로는 관리가 어려워진다.
- Expected behavior: canonical namespace 위에 wildcard 또는 category hierarchy 기반 정책을 둘 수 있어야 한다.
- Current behavior: `_matches_any()` + fnmatch. `provider:*`, `tool:file.*` 등 지원. 하위 호환.
- Severity: medium
- State: fixed
- Owner: unassigned
- Test id: `SO-PERM-001`
- Resolution: `_matches_any()` + fnmatch. `provider:*`, `tool:file.*` 등 지원. 하위 호환.

### GAP-SO-007 — Config Contract ✅ Fixed

- Area: system
- Pattern reference: `docs/patterns/system-overview/02-architecture.md` `105-138`
- Status reference: `docs/status/system-overview.md` `SO-STATUS-007`
- Summary: config 경로 canonical 확정: `.awf.toml` (project) + `~/.config/awf/config.toml` (user). pattern 예시는 reference로 축소.
- Severity: low
- State: fixed
- Owner: unassigned
- Test id: `SO-CONFIG-001`
- Resolution: `load_awf_config()`이 `~/.config/awf/config.toml` (user) → `.awf.toml` (project) 순서로 로드하는 것이 canonical contract. pattern의 넓은 예시는 향후 확장 가능성을 위한 것으로, 현재 구현이 canonical.

## Cross-Area Notes

- `GAP-SO-001`은 `GAP-AS-001`, `GAP-AS-002`, `GAP-AS-004`, `GAP-AS-007`의 상위 기반 gap이다.
  - analysis mode contract와 observation contract를 외부 명세로 일반화하려면 system 차원의 spec loader 확장이 필요하다.
- `GAP-SO-002`와 `GAP-SO-003`은 같은 축의 문제다.
  - search path 확장만으로는 부족하고, spec root 자체가 Claude 전용 경로에서 분리되어야 한다.
- `GAP-AS-003`과 `GAP-AS-005`는 acceptance test 설계 전에 canonical stage semantics를 먼저 고정해야 한다.
- `GAP-SO-005`와 `GAP-SO-006`은 독립적으로 보이지만, 장기적으로 multi-agent/workflow status와도 교차될 가능성이 높다.

## Closing Rule

gap을 닫으려면 아래가 모두 충족되어야 한다.

- 관련 구현 변경 완료
- 관련 `status` 문서 갱신
- 관련 테스트 추가 또는 갱신
- 테스트 통과 확인
