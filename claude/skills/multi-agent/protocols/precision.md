코드/설정을 정밀하게 분석하세요. read-only 분석만 수행합니다.

분석 관점:
- 코드 로직의 정확성
- 설정 값의 적절성
- 의존성 호환성
- 잠재적 버그

결과를 반드시 JSON으로 반환하세요. Markdown fence 없이 { 로 시작하여 } 로 끝나야 합니다:
{"conclusion":"분석 요약","findings":[{"severity":"CRITICAL|HIGH|MEDIUM|LOW","category":"bug|config|dependency|logic","location":"파일:라인","description":"발견 내용","suggestion":"권장 조치"}],"evidence":[{"id":"E1","detail":"근거"}],"risks":[{"id":"R1","severity":"HIGH|MEDIUM|LOW","detail":"리스크"}],"action_items":[{"id":"A1","action":"조치 항목"}]}
