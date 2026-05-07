아래 파일의 코드를 읽고 관찰된 사실만 추출하세요.

## 규칙 (반드시 준수)

- 이 파일만 분석하세요. 다른 파일의 내용을 추측하지 마세요.
- 다른 파일과의 연관은 입출력 인터페이스(타입, 파라미터, 반환값)만 기록하세요.
- 심각도 판정, 결론, 권장 사항을 작성하지 마세요.
- 코드 스니펫을 포함하지 마세요. 위치 참조(파일:행번호)로 대체하세요.
- 이전 파일의 분석 결과를 참조하거나 언급하지 마세요.

## 대상 파일

{xml_bundle}

## 참고: git 변경 이력

{git_history}

## 출력 형식

마크다운 본문을 먼저 작성하고, 말미에 ```json 블록으로 동일한 정보를 구조화하세요.
마크다운에 있는 항목이 JSON에 없거나 그 반대이면 안 됩니다.

### 마크다운 섹션 (아래 순서대로 작성)

# {path}

## 기본 정보
- role: (router|controller|service|dao|entity|model|util|test|config|middleware|component|module|mapper|schema|migration|other)
- language: (typescript|javascript|python|java|kotlin|swift|go|php|rust|sql|xml|graphql|protobuf|other)
- lines: (숫자)

## 엔드포인트
(controller/router인 경우만. 아니면 "해당 없음"으로 표기)
- METHOD /path — auth: guard명|none, params: 목록

## 테이블/엔티티 참조
(다음 패턴 중 하나라도 있으면 테이블로 기록: repository의 tableName 선언, TypeORM @Entity 데코레이터, MyBatis mapper XML의 namespace/FROM/JOIN, QueryBuilder 대상. 없으면 "해당 없음"으로 표기)
- TABLE_NAME (읽기|쓰기|읽기/쓰기: 용도 1줄)

## 외부 서비스 호출
(외부 서비스 호출이 있는 경우만. 없으면 "해당 없음"으로 표기)
- ServiceName.method() → 대상 (SQS|Redis|HTTP|gRPC 등)

## 의존성 (import)
- ClassName (경로 또는 패키지명)

## 비즈니스 로직 요약
(메서드별로 정상 흐름 / 에러 처리 / 경계 조건을 분리하여 단계별로 기술)

### methodName
**정상 흐름**: 입력 → 처리 단계 → 출력
**에러 처리**: try-catch, 조기 반환, fallback 경로
**경계 조건**: 빈 입력, null, 상한/하한, 부수 효과

## 변경 이력 요약
(git history가 있는 경우, 사실만 기록. 변경의 추정 원인이나 의도 해석은 포함하지 않음)

## 관찰된 신호
(코드에서 보이는 패턴을 사실로만 기록 — 심각도 없음)
- 사실 기술

### JSON 블록 (마크다운과 동일 의미, 기계 파싱용)

```json
{{
  "path": "{path}",
  "role": "<role>",
  "language": "<language>",
  "lines": <number>,
  "endpoints": [
    {{"method": "<METHOD>", "path": "<path>", "auth": "<guard|none>", "params": ["<param>"]}}
  ],
  "tables": [
    {{"name": "<TABLE_NAME>", "access": "<read|write|read_write>", "usage": "<용도>"}}
  ],
  "external_calls": [
    {{"service": "<ServiceName>", "method": "<method>", "target": "<대상>"}}
  ],
  "imports": [
    {{"name": "<ClassName>", "source": "<path>"}}
  ],
  "business_logic": [
    {{"method": "<methodName>", "steps": ["<단계>"]}}
  ],
  "signals": [
    "<관찰된 사실>"
  ]
}}
```

필수 필드: path, role, language, lines, imports, business_logic, signals (빈 배열이라도 포함)
조건부 필드: endpoints (controller/router인 경우)
선택 필드: tables, external_calls (해당 없으면 생략 가능)
