빠르게 분석하세요. read-only 분석만 수행합니다.

분석 관점:
- 전체 구조 파악
- 주요 이슈 식별
- 핵심 데이터 추출

결과를 반드시 JSON으로 반환하세요. Markdown fence 없이 { 로 시작하여 } 로 끝나야 합니다:
{"conclusion":"분석 요약","findings":[{"severity":"CRITICAL|HIGH|MEDIUM|LOW","category":"...","location":"...","description":"...","suggestion":"..."}],"evidence":[{"id":"E1","detail":"..."}],"risks":[{"id":"R1","severity":"...","detail":"..."}],"action_items":[{"id":"A1","action":"..."}]}
