# talon

한국 주식 단타·스윙을 위한 개인용 투자 에이전트. 설계 전모는 `docs/DESIGN.md`, 용어는 `CONTEXT.md`, 결정 근거는 `docs/adr/`, 오픈소스 리서치는 `docs/research/SYNTHESIS.md`를 먼저 읽을 것.

절대 원칙:

- 매매 결론은 결정론적 퀀트 코어만 생성한다. LLM은 리서치·게이트(축소/보류만)·해설 담당 (ADR 0001, 0002)
- 백테스트 경로에 LLM 호출 금지 (ADR 0002)
- LLM 호출은 Claude 구독 CLI 경로만. `ANTHROPIC_API_KEY` 설정 금지 (ADR 0009)
- 리스크 게이트는 하드 게이트 — 어떤 층도 우회 불가 (ADR 0006)
- 팩터 표현식에 `eval()` 금지, 시계열 처리는 룩어헤드 안전성이 최우선 (ADR 0005, 0010)
