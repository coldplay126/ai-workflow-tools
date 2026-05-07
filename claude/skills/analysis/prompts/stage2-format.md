## 출력 형식 레퍼런스

### api-spec.json
반드시 pretty-print된 JSON으로 출력하세요 (indent=2).
{
  "domain": "...",
  "endpoints": [
    {
      "method": "",
      "path": "",
      "summary": "",
      "auth": "",
      "params": {},
      "queryParams": {},
      "responses": {},
      "businessLogic": ["단계별 로직"],
      "subFlows": { "내부함수명": ["단계별 하위 흐름"] }
    }
  ],
  "guards": [],
  "dtos": []
}

### data-model.md
# 데이터 모델
## 테이블 (자체 소유)
각 테이블의 전체 컬럼, 타입, 설명을 포함하세요.
## 참조 테이블 (다른 도메인)
## Redis 키 패턴
## 엔티티 관계도 (Mermaid erDiagram 권장)

### domain-overview.md
# {unit} 개요
## 목적
이 unit이 무엇을 하는지 1-2문단으로 서술. 기술 용어뿐 아니라 비즈니스 맥락도 포함.
## 소스 위치
파일별 역할을 표로 정리.
## 핵심 비즈니스 흐름
각 주요 흐름마다 단계별 설명 + ASCII 흐름도를 모두 포함.
## 의존성 구조 (트리 형태)
## 주요 개념
도메인 특화 개념, enum/상수 값 목록 등 참조 정보.

### external-integration.md
# 외부 연동
각 연동 서비스별로:
- 용도, 호출 시점, 데이터 형식
- 장애 처리 방식 (없으면 "없음"이라고 명시)
## 데이터 흐름도 (주요 API별 ASCII 흐름도)
## 환경변수
## 순환 의존성 여부
