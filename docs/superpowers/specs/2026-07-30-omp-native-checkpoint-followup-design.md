# OMP Native Checkpoint Follow-up 설계

## 배경

`awf agents followup-omp --run`은 현재 AWF schema-v2 dispatch provenance만 읽는다. 사용자가 같은 dispatch 디렉터리에 생성되는 `omp-native-<fingerprint>.json` checkpoint를 전달하면 해당 파일이 실제 OMP 실행 기록임에도 `not an OMP provenance record` 오류가 발생한다.

Native checkpoint에는 coordinator session과 worker handle이 있으므로 후속 실행에 필요한 정보가 이미 존재한다. 별도의 변환 파일을 만들지 않고 이를 직접 지원한다.

## 목표

- `--run <omp-native-checkpoint> --role <role>`로 정확한 worker를 선택한다.
- `--task-id <id>` 검색에 native checkpoint worker를 포함한다.
- 기존 schema-v2 dispatch provenance 동작과 출력 형식을 유지한다.
- 후속 결과는 기존 schema-v2 provenance로 기록해 lineage를 보존한다.
- 모호하거나 불완전한 checkpoint는 fail-closed 처리한다.

## 비목표

- Native checkpoint 파일을 schema-v2 파일로 변환하거나 수정하지 않는다.
- Worker가 없는 checkpoint에서 task를 추측하지 않는다.
- 종료된 worker 자체를 되살린다고 보장하지 않는다. 기존 정책대로 registry가 없으면 history 기반 successor를 생성한다.
- OMP checkpoint schema 자체는 변경하지 않는다.

## 선택한 접근

Native checkpoint를 읽는 시점에 내부 호환 view로 정규화한다.

다른 대안은 다음 이유로 제외한다.

1. 오류 안내만 개선: 사용자가 직접 올바른 provenance를 찾아야 하므로 이미 존재하는 checkpoint 정보를 활용하지 못한다.
2. 연관 schema-v2 provenance 자동 탐색: 동일 session 또는 task를 가진 후보가 여러 개일 수 있어 잘못된 run을 선택할 위험이 있다.
3. Native checkpoint 직접 지원: 명시적으로 전달된 파일만 사용하며 자동 탐색보다 결정적이다. 이번 설계로 채택한다.

## 입력 판별과 정규화

`lookup_omp_provenance`는 다음 두 형식을 허용한다.

### Schema-v2 dispatch provenance

- `backend == "omp"`
- `schema_version == 2`
- 기존 payload를 변경 없이 반환한다.

### Native checkpoint

- `kind == "omp_native_batch"`
- `version == 1`
- `batch_fingerprint`, `coordinator_session_id`, `workers`가 유효해야 한다.

Native checkpoint는 메모리에서 다음 호환 필드로 정규화한다.

- `backend`: `"omp"`
- `schema_version`: `2`
- `run_id`: checkpoint 파일 stem인 `omp-native-<fingerprint>`
- `coordinator_session_id`: checkpoint 값
- `status`: checkpoint `state`
- `agents`: worker record 기반 호환 배열
- `source_kind`: `"omp_native_batch"`

파일은 수정하지 않는다.

## Worker 식별

### `--run` + `--role`

기존 schema-v2에서는 agent record의 `role`을 직접 비교한다.

Native checkpoint에는 role 필드가 없으므로 worker 배열의 각 index에 대해 `omp_worker_name(index, role)`을 계산하고 checkpoint의 `name` 또는 `task_id`와 정확히 비교한다.

- 일치 0개: role not found
- 일치 1개: 해당 worker 선택
- 일치 2개 이상: ambiguous 오류

문자열 부분 일치나 추정은 사용하지 않는다.

### `--task-id`

Schema-v2 records와 native checkpoint workers를 함께 검색한다. 정확히 한 record만 허용한다. 여러 파일에서 같은 task ID가 발견되면 기존 정책대로 ambiguous 오류를 반환한다.

## Actionable target 검증

Native worker 호환 record에는 다음 값이 포함되어야 한다.

- persisted coordinator session
- `task_id`
- `agent_uri` 또는 `history_uri`

Checkpoint의 `session_persisted`가 true인 경우 각 worker 호환 record에 전달한다. 하나라도 부족하면 기존 `_require_actionable_target` 오류 경로를 사용한다.

## Follow-up lineage

Native checkpoint에서 시작한 follow-up도 기존 `write_omp_dispatch_provenance`를 사용한다.

- `parent_run_id`: checkpoint 파일 stem
- `parent_task_id`: 선택된 native worker task ID
- direct 또는 successor delivery 기록
- 후속 provenance schema는 항상 version 2

## 오류 처리

- JSON 파싱 실패: `invalid OMP provenance file`
- 지원하지 않는 JSON: `not a supported OMP provenance record`
- native 필수 필드 누락: 누락 필드를 명시한 `invalid OMP native checkpoint`
- role 불일치: checkpoint run ID를 포함한 `role ... not found`
- 중복 role/task: ambiguous 오류
- session 미저장 또는 handle 누락: 기존 actionable target 오류

모든 오류는 네트워크 호출 전에 발생한다.

## 테스트

1. Native checkpoint path와 role로 worker를 선택한다.
2. Native checkpoint path에서 잘못된 role을 거부한다.
3. Native checkpoint의 exact task ID를 전역 검색한다.
4. 여러 checkpoint의 중복 task ID를 ambiguous로 거부한다.
5. session 또는 worker handle이 누락된 checkpoint를 거부한다.
6. 기존 schema-v2 path와 task ID 동작이 유지된다.
7. CLI follow-up fixture에서 native checkpoint가 coordinator resume 입력으로 전달되고 schema-v2 follow-up provenance를 기록한다.
8. 실제 OMP E2E에서 successor delivery와 parent lineage를 확인한다.

## 완료 기준

- `awf agents followup-omp --run omp-native-<fingerprint>.json --role <role>`가 성공한다.
- 같은 worker를 `--task-id`로도 선택할 수 있다.
- 후속 provenance가 native checkpoint stem과 task ID를 parent lineage로 기록한다.
- 기존 follow-up 테스트와 AWF 전체 회귀 테스트가 통과한다.
- 실제 fixture 호출에서 원본 checkpoint와 결과 파일이 변경되지 않는다.
