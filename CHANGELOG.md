# Changelog

이 프로젝트의 사용자에게 영향을 주는 주요 변경사항을 기록합니다. 버전은
`cli/pyproject.toml`의 `awf-cli` 패키지 버전을 따릅니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 기준으로
합니다.

## [Unreleased]

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
