# Changelog

이 프로젝트의 사용자에게 영향을 주는 주요 변경사항을 기록합니다. 버전은
`cli/pyproject.toml`의 `awf-cli` 패키지 버전을 따릅니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 기준으로
합니다.

## [Unreleased]

### Fixed

- code fallback 후 Judge evidence provenance를 최초 Writer 결과가 아니라 실제 재실행 Writer 결과와 대조해 잘못된 `unknown_claim_id`·`evidence_modified` 경고를 방지합니다.
- Review/Verify의 multi-LLM conflict 조건이 더 이상 자동 PASS하지 않고, malformed
  또는 grounded conflict evidence를 fail-closed로 판정합니다.

### Added
- `awf wt sync --from <production> --to <staging>`가 configured production의
  source-only delta를 latest staging에 preview/apply하고, clean 3-way 결과와 Git
  mode를 보존합니다. Prepare/production verify와 publication 전후 remote SHA를
  재검증하며, 중단된 clean publication은 같은 verified lease로 재개합니다. 이미
  포함된 내용은 `noop`, 충돌은 preserved worktree입니다. Sync PR의 reserved
  branch 및 `AWF-No-Promote: true` provenance는 `wt promote`와 `wt release
  add`에서 거부됩니다. Exact promotion의 누락 delta는
  `staging_missing_main_delta`와 경로·sync 명령으로 진단됩니다.
- `awf wt compact` adds preview-first, all-or-nothing ignored-path compaction for
  stale AWF `PR_OPEN`/`DEPLOYING`/`DEPLOYED`/`CLEANABLE` worktrees. It reports
  allocated bytes and entry counts, takes a nonblocking repository lock, fully
  revalidates all candidates before deletion, and preserves all lease/registry,
  branch, worktree, Git HEAD, and Git status lifecycle state.

- `awf wt release open|add|seal|publish`로 처음부터 누적 release bridge를
  관리합니다. `open`은 최신 production 기준 `PROMOTE` lease를 만들고, `add`는
  staging merge 순서의 immutable source base/head/merge/path pin만 누적합니다.
  `seal`은 latest-target 재구성과 prepare/production verify 뒤 source를 잠그며,
  `publish`는 target drift 시 같은 managed worktree에서 pin delta를 재구성·재검증한
  뒤 정확히 하나의 production PR을 열거나 재사용합니다. duplicate/reordered
  source, sealed 이후 add, provenance drift, verify/PR mismatch는 fail-closed입니다.
- Plan phase now records material, recommendation-first design choices in the
  canonical `.workflow/artifacts/planning-options.json` artifact. A required
  unselected option routes G1 through `user_decision`/`deciding`; users select
  through `awf wf select-option --decision-id ... --option-id ... --actor ...
  --repo-root . --json`. Selected artifacts are plan-rerun inputs, while a
  changed post-G1 selection re-plans the workflow and preserves the audit
  history.
- `awf wf seal-plan --repo-root . --json` host-seals the current selected or
  no-decision Planning Options artifact to exactly six regenerated Plan
  artifacts, including `allowed-files.json`. Selection changes archive six
  outputs plus active provenance before G1 rejects missing, malformed, or stale
  provenance.
- `awf wf approve --decision approve|revise|reject --actor ...`가 provider나
  OMP worker를 거치지 않고 parent-only G3 scope hash, approval artifact,
  state/history를 결정론적으로 기록합니다.
- `awf wf confirm --decision complete|hold --actor ...`가 G6 이후 Done을
  provider에 위임하지 않고 interactive parent-only confirmation artifact와
  state/history로 기록합니다. approval/Done CLI는 interactive TTY를 요구합니다.
- `awf wf autoresearch-register --result-json ...`가 G3 이후 Impl에서 완료된
  OMP Autoresearch 결과를 Planning Options·scope hash·allowed files·metrics
  digest에 결합해 등록합니다. Autoresearch score는 G4/G5/G6을 대체하지 않습니다.
- `awf wf autoresearch-schema --json`이 등록 envelope의 exact versioned JSON
  Schema를 출력합니다. G3 six-artifact seal은 Impl 진입, isolated patch 적용,
  Autoresearch 등록, G5 scope-check에서 다시 검증됩니다.
- OMP team 설정에 opt-in Plan baseline research, Review lens, disjoint
  write-scope Impl isolation을 추가하고, agent compiler가 LSP/AST,
  Browser/Debug, Security Scan 도구 이름을 지원합니다.
- Done/status가 redacted OMP provenance, cancellation/partial/checkpoint,
  patch-scope, follow-up lineage와 출처가 분리된 worker usage를 read-only로
  표시합니다.
- `awf lsp setup|status|materialize`가 repository 언어와 local binary를 감지하고,
  machine-readable preview 뒤 명시적 apply로 user OMP config, shared local profile,
  Python `src` layout용 ignored `pyrightconfig.json`, Git common-dir exclude,
  managed-worktree prepare와 safe OMP isolation을 구성합니다. Binary 설치와
  repository `.gitignore` 변경은 수행하지 않습니다.

### Documentation

- `awf lsp`의 preview/apply, local-only profile, linked-worktree materialize,
  OMP isolation fallback, fail-closed custom prepare 정책을 neutral reference와
  installable skill로 문서화했습니다.
- OMP 18.0.6 top-level main/managed worktree LSP 검증과, child tool inventory가
  LSP를 노출하지 않을 때 parent 진단으로 전환하는 fallback을 문서화했습니다.
  실제 local config는 Git에 커밋하지 않습니다.

### Changed

- 단독으로 관리하는 `ai-workflow-tools`는 source PR review 정책을
  `approved_or_self_merged`로 설정해 별도 리뷰어 없이 작성자 본인의 staging
  PR을 승격할 수 있습니다. source checks와 production verification은 유지합니다.
- 다중 production promotion은 더 이상 이전 PR merge SHA와 다음 PR base SHA의
  완전 일치를 요구하지 않습니다. 모든 source merge SHA와 staging merge 순서는
  계속 검증합니다. 명시한 PR delta만 최신 production 기준 단일 브랜치에 적용하며,
  사이의 staging-only commit은 제외합니다.

- exact promotion을 기본으로 유지하며, 선행 staging 변경 A를 production에
  포함할 수 없는 단일 merged PR B에는 opt-in `--out-of-order`를 추가했습니다.
  이 경로는 하나의 `--source-pr`만 받고 `--exclude-path`를 허용하지 않으며,
  위반 시 `invalid_out_of_order_promotion`, rename이면
  `unsupported_out_of_order_rename`으로 중단합니다. B가 A의 API, schema,
  behavior에 의존하면 중단해야 합니다. 충돌은 `out_of_order_conflict`로
  보존하고 AWF가 보고한 파일만 수정한 뒤 같은 preview/apply를 재실행합니다.
  source 또는 target SHA가 바뀌면 `promotion_provenance_changed`로 중단합니다.
  synthetic production PR은 별도 approval과 successful checks를 거쳐야 합니다.
  staging squash commit은 promotion input이
  아니며 direct staging squash cherry-pick은 금지합니다.

- plan/verify/test에 `awf wf db-check --stage ... --json`을 일반 gate 직전에
  연결했습니다. database signal이 있으면 production schema가 mandatory이고
  same-engine local은 DDL/planner evidence에, DuckDB는 profiling/equivalence
  분석에 사용합니다. project-specific replica sample은 opt-in만 허용하며 raw primary rows는 금지합니다.
  local test command가 없을 때만 reason/approver/timestamp
  waiver를 사용합니다. CLI는 database driver, masking, replica provisioning을
  제공하지 않고 project-owned sanitized JSON evidence를 검증합니다.
- OMP 전문 도구 evidence는 optional capability이며 unavailable 상태를 PASS로
  대체하지 않습니다. Verify/Test와 deployment health의 최종 판정은 계속
  parent deterministic gate와 실제 rollout evidence가 소유합니다.

## [0.1.6] - 2026-08-13

### Fixed

- 유효한 `api-spec.json`이 문서 전체를 감싼 정확한 `json` Markdown fence 하나로 반환되면 fence를 제거한 뒤 JSON artifact로 게시합니다.
- malformed JSON이나 fence 밖 설명은 정규화하지 않고 top-level object가 아닌 JSON도 거부하며, consistency 실패 시 결합 결과를 `fanout-consistency` 진단 artifact로 보존합니다.
- Judge가 새 merged claim ID를 만들 때 `original_claims`의 Writer-qualified reference를 검증해 정상 병합을 `unknown_claim_id`로 오진하지 않습니다.
- 전체 fanout 경과 시간이 Stage 2 event와 최종 JSON envelope까지 전달되는 provider-backed 통합 회귀를 추가했습니다.

### Documentation

- JSON fence 정규화 경계, consistency failure artifact, merged claim provenance, elapsed telemetry 계약을 CLI와 analysis reference에 반영했습니다.

## [0.1.5] - 2026-08-13

### Fixed

- Stage 2 `parallel_v2` fanout이 consistency 실패한 결과를 성공으로 게시하지 않고 single-agent Stage 2로 fallback합니다.
- 성공한 fanout의 실제 monotonic 경과 시간을 Stage 2 event와 JSON envelope `elapsed_sec`에 기록합니다.
- service-level `--check`와 `--catalog` 경로 해석에서 가짜 `__placeholder__` domain discovery와 경고를 제거했습니다.

### Documentation

- Writer/Judge fanout 완료 조건, consistency fallback, elapsed telemetry, service-level 경로 해석 계약을 CLI와 analysis reference에 반영했습니다.

## [0.1.4] - 2026-08-13

### Fixed

- source hash baseline을 성공한 final output 뒤에만 갱신해 provider 실패가 마지막 성공 baseline을 덮지 않도록 했습니다.
- JSON-mode analysis의 stdout redirect를 mutation scope로 제한하고 최종 stdout에는 envelope 하나만 기록합니다.
- cross 실패 시 `auto_downgrade()`가 선택한 precise 또는 solo target을 실제로 한 번 실행하고 mode와 reason에 같은 target을 남깁니다.
- high-severity finding 비교에 정규화된 위치와 description을 포함합니다.
- `--yolo` permission mode를 fanout provider instance마다 적용합니다.
- Claude Code와 Codex의 streaming 실행이 non-streaming과 같은 effort, schema, add-dir, stdin 계약을 사용합니다.

### Documentation

- success-only hash baseline, JSON stream, downgrade, finding signature, fanout permission, streaming option 계약을 README와 analysis/multi-agent 문서에 반영했습니다.

## [0.1.3] - 2026-08-13

### Fixed

- 비어 있거나 malformed인 Stage 2 fanout writer 설정을 구조화 fallback으로 변환하고 provider 실행 실패의 기존 진단은 보존합니다.
- `awf analyze --all` child의 exit code `130`을 후속 domain이나 delay 없이 즉시 전파합니다.
- 같은 service/domain의 mutating analysis를 nonblocking lock으로 직렬화해 중복 provider 실행과 state 경합을 막습니다.

### Documentation

- fanout fallback, same-domain exclusive mutation, `--all` 취소 전파 계약을 CLI, reference, pattern 문서에 반영했습니다.

## [0.1.2] - 2026-08-13

### Fixed

- 멀티에이전트 결론을 normalized prefix로 분류해 명시적 FAIL을 문장 안의 PASS 문자열 때문에 통과시키지 않습니다.
- worker timeout을 provider 호출에 전달하고 `returncode == 124`를 timeout으로 기록합니다.
- OMP native/print의 required JSON 결과가 object가 아니면 fail closed로 처리하며 raw evidence는 보존합니다.
- `AgentResult`와 team artifact 저장 경로가 non-object parsed 값을 성공으로 합성하지 않도록 방어했습니다.

### Documentation

- judge precedence, timeout inheritance, OMP JSON object 경계 계약을 CLI, reference, pattern 문서에 반영했습니다.

## [0.1.1] - 2026-08-13

### Fixed

- 현재 Stage 2 attempt가 모든 required output을 제공하지 않으면 이전 output 파일이 남아 있어도 분석을 failed로 유지합니다.
- Stage 2 저장 result는 동일 source/config generation에서만 재사용하고, required Stage 3 실패의 state와 diagnostic artifact를 보존합니다.
- Stage 2/3 성공과 새 source/config generation 시작 시 해당 `retryCount`가 reset되도록 정리했습니다.

### Documentation

- analysis resume, generation, Stage 3 failure, retry budget 상태 계약을 CLI, reference, pattern, skill 문서에 반영했습니다.

## [0.1.0] - 2026-08-13

첫 문서화된 릴리스 스냅샷입니다. 이 체인지로그를 만들기 전에는 Git 태그와
릴리스별 변경 기록이 없었습니다.

### Added

- `awf ready`와 `awf doctor`를 통한 read-only 저장소 진단, 자동화 수준 판정,
  다음 명령 추천.
- `scan`, `analyze`, `check`, `catalog`로 구성된 증분 분석 파이프라인과
  `.ai-context` 산출물 계약.
- `plan → review → approve → impl → verify → test → done` 순서의 7단계 gated
  workflow와 `.workflow` 상태 및 artifact 관리.
- Claude, Codex, Gemini, OpenAI, subprocess provider를 같은 결과 계약으로
  다루는 provider adapter와 review/verify 결과 synthesis.
- OMP host-native `task`/`hub`, OMP NDJSON adapter, `cmux-agent`, legacy Pi 실행
  surface와 worker별 모델 라우팅 및 checkpoint provenance.
- 운영 evidence와 결정 기록을 관리하는 `awf wiki` 명령군.
- feature lease 생성·재사용, merged PR 연결, ordered staging PR promotion,
  reviewed-path 제외, finish, evidence-gated GC를 제공하는 `awf wt` 명령군.
- Claude Code, Agent Skills, OMP skill root에 번들 skill과 agent 정의를 연결하는
  `setup.sh` 설치 흐름.

### Changed

- production promotion이 staging branch 전체가 아니라 순서가 검증된 source PR
  delta만 적용하도록 제한되었습니다.
- OMP native 실행이 역할별 모델 override를 전달하고, 같은 agent type에 상충하는
  모델이 지정되면 `omp_worker_model_conflict`로 중단하도록 강화되었습니다.
- release worktree lifecycle이 preview-before-apply, exact PR provenance,
  deployment-health evidence를 모든 변경·정리 작업의 전제조건으로 사용합니다.

### Fixed

- target branch가 전진한 뒤 blocked promotion을 재개할 때 path/blob 검증과 source
  provenance를 보존하도록 recovery 경로를 강화했습니다.
- prepare, production verification, publication failure 재시도가 dirty worktree나
  불일치한 promotion commit을 재사용하지 않도록 fail-closed 조건을 보완했습니다.
- `setup.sh`가 정상적인 skill 기반 설치를 legacy command 누락으로 잘못 경고하던
  문제를 수정했습니다.
- OMP multi-model dispatch에서 role model 설정과 실제 worker override가 어긋나던
  문제를 수정했습니다.

### Documentation

- README, CLI guide, architecture/reference 문서, workflow/agent skill, 재사용
  snippet을 현재 OMP, analysis, workflow, worktree 동작에 맞춰 동기화했습니다.
