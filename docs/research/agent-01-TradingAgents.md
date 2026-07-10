# TradingAgents (TauricResearch) 정밀 분석 — talon 설계 관점

- 레포: https://github.com/TauricResearch/TradingAgents
- 분석 시점 버전: v0.3.1 (2026-07-05 릴리스), 활발히 유지보수 중
- 라이선스: Apache-2.0 / 언어: Python 3.10+ / 규모: ~8.4MB, 134 py 파일
- 판정: **패턴만 빌림 (borrow)** — 코드 채택은 부적합, 오케스트레이션 구조는 참고 가치 높음

## 1. 한 줄 요약

TradingAgents는 "실제 트레이딩 회사의 조직 구조를 LLM 멀티에이전트로 모사"하는
**리서치 스캐폴드**다. 애널리스트→리서처 토론→트레이더→리스크 토론→포트폴리오 매니저로
이어지는 LangGraph 파이프라인이 종목 1개·날짜 1개에 대해 Buy/Hold/Sell 등급을 산출한다.
**백테스팅 엔진도, 체결 모델도, 브로커 어댑터도, 포지션 사이징 집행도 없다.** README가 말하는
"simulated exchange"는 마케팅 문구일 뿐 실제로는 텍스트 등급만 나온다.

## 2. 아키텍처 (LangGraph StateGraph)

핵심은 `tradingagents/graph/`. `TradingAgentsGraph.propagate(ticker, date)`가 진입점이며
`StateGraph(AgentState)`를 컴파일해 실행한다. 노드 흐름(`graph/setup.py`):

```
START → [Market → Social → News → Fundamentals] (순차, 각자 tool 루프)
     → Bull ⇄ Bear (토론) → Research Manager
     → Trader
     → Aggressive ⇄ Conservative ⇄ Neutral (리스크 토론) → Portfolio Manager → END
```

- **애널리스트 4종**: 각자 `bind_tools`된 quick LLM. `should_continue_*`
  (`conditional_logic.py`)가 `last_message.tool_calls` 유무로 ToolNode 재호출 또는
  종료를 결정하는 ReAct 루프. 툴은 카테고리별 `ToolNode`로 묶임(market/social/news/fundamentals).
- **연구 토론**: Bull/Bear가 `investment_debate_state.count`가 `2*max_debate_rounds`에
  도달할 때까지 번갈아 발언. 상태를 문자열 누적(`history`)으로 관리 — 벡터 메모리 아님.
- **리스크 토론**: Aggressive/Conservative/Neutral 3인이 `3*max_risk_discuss_rounds`까지 순환.
- **의사결정 3노드만 구조화 출력**: Research Manager·Trader·Portfolio Manager는
  Pydantic 스키마(`agents/schemas.py`)로 `with_structured_output`을 쓰되, 실패 시
  free-text로 graceful fallback(`invoke_structured_or_freetext`). 나머지는 자유 산문.
- **라우터 안전장치**: 공유 조건부 엣지가 전체 path_map을 매핑(#1088)해 프롬프트/i18n drift로
  라벨이 어긋나도 그래프가 죽지 않게 방어. LangGraph 운용 노하우로 참고할 만함.

## 3. LLM 오케스트레이션 (talon 핵심 관심사)

### 역할 분담과 모델 계층
2단 모델 구성: `deep_think_llm`(Research Manager·PM 등 판단 노드)과
`quick_think_llm`(애널리스트·토론). 기본값은 OpenAI `gpt-5.5`/`gpt-5.4-mini`.

### 프롬프트 설계
- 시스템 프롬프트에 지표 카탈로그(SMA/EMA/MACD/RSI/BOLL/ATR/VWMA)를 통째로 넣고
  "최대 8개, 중복 회피"를 지시하는 식의 **도메인 지식 임베딩**이 특징. market_analyst 프롬프트가 대표적.
- 구조화 출력에서는 **Pydantic 필드 description을 출력 지시문으로 재활용**해 프롬프트 본문은
  맥락만 담게 함 — 프롬프트/스키마 관심사 분리가 깔끔하다(참고 가치 있음).
- `get_language_instruction()`으로 전 에이전트 출력 언어를 config(`output_language`)로 전환.
  **한국어(한국어)가 CLI 선택지에 이미 존재.** 단, 내부 토론은 추론 품질을 위해 영어 유지 권장.

### 토론/합의 메커니즘
진짜 합의 알고리즘은 아니다. 고정 라운드 수만큼 페르소나가 번갈아 산문을 누적하고,
"심판" 노드(Research Manager, Portfolio Manager)가 그 히스토리를 읽어 등급을 확정한다.
반대 관점을 강제 노출시켜 확증편향을 줄이는 **디베이트 프롬프팅** 패턴이 본질.

### 메모리·반성 (Reflection) — 이 레포의 진짜 강점
`agents/utils/memory.py`의 `TradingMemoryLog`가 **append-only 마크다운 로그** 하나로
전체 메모리를 구현. 벡터 DB·임베딩 없음.
- Phase A: 매 run 종료 시 결정을 `pending` 태그로 append (LLM 호출 0).
- Phase B: 같은 종목 다음 run 시작 시, yfinance로 보유기간(기본 5일) 실현수익률과
  벤치마크 대비 알파를 계산 → `Reflector`가 2~4문장 반성문 생성 → 로그의 pending을
  결과+반성으로 원자적 교체(temp+os.replace). 5-tier 등급도 태그에 기록.
- 다음 분석 때 동일 종목 최근 5건 + 교차종목 최근 3건 교훈을 PM 프롬프트에 주입.
지연 반성(deferred reflection) + 알파 기반 자기평가 루프는 talon에 그대로 이식할 가치가 큼.

### LLM 호출량·비용 구조 (구독제 제약과 직결)
단일 종목·단일 날짜 1회 분석에 **대략 16~20회 LLM 호출**(그중 2회는 고비용 deep 모델):
애널리스트 4×~2-3, Bull/Bear 2, RM 1(deep), Trader 1, 리스크 3, PM 1(deep), +다음 run 반성 1.
`cli/stats_handler.py`가 콜백으로 호출수·토큰을 집계. **결정 하나가 수만 토큰**을 쓰는
토큰 다소비 구조이며, 백테스트를 날짜별로 돌리면 비용이 선형 폭증한다.

## 4. 데이터 파이프라인 & 룩어헤드 방지

- 벤더 추상화(`dataflows/interface.py` + `config.py`): 카테고리/툴 단위로 벤더 체인 지정
  (`yfinance`/`alpha_vantage`/`fred`/`polymarket`). 명시한 벤더 외 silent 라우팅 안 함.
- **룩어헤드 방지가 명시적**: `load_ohlcv(symbol, curr_date)`가 `Date <= curr_date`로 필터,
  fundamentals도 회계기간 컬럼을 날짜로 잘라냄(#1115), 뉴스도 미래일자 차단(`test_news_lookahead`).
  "backtest 시 미래가격 안 보이게"가 코드 주석·테스트로 강제됨 — talon 백테스터 설계 시 준수해야 할 규율.
- **환각 방지 그라운딩**: `market_data_validator.get_verified_market_snapshot`이 결정론적
  OHLCV/지표 스냅샷을 만들고, 애널리스트에게 "정확한 수치 주장은 이 스냅샷을 진실로 삼으라"고 지시(#830).
  종목 정체성도 실행 전 yfinance로 결정론적 확정(#814). **LLM 수치 환각 억제 패턴으로 매우 유용.**
- 체크포인트: 옵션(`--checkpoint`), 노드별 SqliteSaver로 크래시 재개. thread_id에
  애널리스트 선택·토론 깊이·자산모드를 fold-in해 그래프 형태가 바뀌면 새로 시작(#1089).

## 5. 없는 것 (중요)

- **백테스팅 엔진 없음.** `backtrader`가 의존성에 선언돼 있으나 코드 어디에서도 import되지 않는
  **사문화된(vestigial) 의존성**이다. 이벤트/벡터 엔진, P&L 회계, 포트폴리오 시뮬레이션 전무.
  CLI는 `analysis_date` 단일 날짜만 처리하고 날짜 루프도 없다.
- **체결/수수료/슬리피지 모델 없음.** Trader/PM의 entry_price·stop_loss·position_sizing은
  전부 LLM이 채우는 **텍스트 필드**일 뿐 집행·검증되지 않는다.
- **라이브 트레이딩 어댑터 없음.** 브로커 주문 연동 0. 산출물은 마크다운 리포트 트리 + 등급.
- **정량 리스크 관리 없음.** "리스크"는 3인 페르소나의 정성 토론이지 VaR·변동성 한도·상관 익스포저 계산이 아님.

즉 TradingAgents는 **"리서치·근거 생성 레이어"** 만 담당하는 도구다. talon의 요구
(백테스팅→페이퍼→실전→자동매매)의 앞단 1/4만 커버한다.

## 6. talon 적용성 평가

### 그대로 채택(adopt): 없음
- LLM 레이어가 langchain-openai/anthropic 등 **전부 API 키 종량 과금** 기반이다.
  `create_llm_client`는 provider별 `ChatOpenAI`/`ChatAnthropic`를 생성할 뿐,
  **Claude Max 구독(Claude Code/Agent SDK) 인증 경로가 전혀 없다.** talon의 절대 제약과 정면 충돌.
  코드를 그대로 쓰면 첫 실행부터 종량 과금이 발생한다 → 채택 불가.

### 패턴만 빌림(borrow)
1. **애널리스트→토론→심판 그래프 토폴로지**: 역할 분담과 조건부 라우팅 구조는 talon의
   리서치 에이전트 설계 뼈대로 훌륭. 단 LangGraph 대신 Claude Agent SDK/서브에이전트로 재구현.
2. **지연 반성 + 알파 기반 메모리 로그**: append-only 마크다운, pending→resolve 2단계,
   실현 알파로 자기평가. 벡터DB 없이 저렴하게 "학습 루프"를 만드는 talon 페이퍼트레이딩 단계에 이상적.
3. **결정론적 검증 스냅샷 그라운딩**: LLM이 인용하는 수치를 코드가 만든 ground-truth에 앵커링.
   차트/지표 분석에서 환각 억제 필수 패턴.
4. **룩어헤드 규율**: `data <= curr_date` 강제 + 테스트. talon 백테스터의 제1 원칙으로 이식.
5. **Pydantic 스키마 = 출력 지시문 + free-text fallback**: 구조화 출력을 강제하되 실패에 관대한 설계.
6. **디베이트 프롬프팅**: Bull/Bear·리스크 3페르소나로 확증편향 완화. 단타/스윙 판단의 반대근거 강제에 유용.

### 참고만(reference)
- 벤더 추상화 레이어 구조(카테고리/툴별 벤더 체인)는 talon의 토스 OpenAPI 어댑터 설계 시 인터페이스 참고.
- 체크포인트 thread_id에 그래프 형태를 fold-in하는 방식.

### 피할 것(avoid)
- **비용 구조**: 결정당 16~20 LLM 호출은 단타/스윙에서 종목·시점이 많아질수록 감당 불가.
  구독제라도 rate limit·컨텍스트 한도에 부딪힌다. talon은 호출 수를 1자릿수로 압축해야 함.
- **backtrader 미사용 의존성**·마케팅성 "simulated exchange" 문구에 현혹되지 말 것.
  이 레포는 백테스팅을 **회피**했지, 해결하지 않았다.
- LLM을 매매 실행 경로에 직접 두는 구조(재현 불가·감사 곤란). talon은 시그널/집행을
  결정론 코드로 분리해야 함.

### 한국 시장 적용성
- **가격 데이터**: yfinance가 `.KS`(KOSPI)/`.KQ`(KOSDAQ)를 지원하므로 OHLCV는 동작.
  다만 talon은 토스 OpenAPI를 쓸 것이므로 이 레이어는 재작성 대상.
- **벤치마크 공백**: `benchmark_map`에 `.KS`/`.KQ`→KOSPI/KOSDAQ 매핑이 없어 알파 계산이 SPY로
  잘못 폴백된다. 이식 시 `^KS11`/`^KQ11` 추가 필요(사소).
- **뉴스·센티먼트가 미국 편향**: StockTwits·Reddit·Alpha Vantage 뉴스·FRED(미국 매크로)·Polymarket
  전부 미국 중심. **한국 종목 센티먼트·뉴스 커버리지가 사실상 공백**이다. talon은 네이버 금융/DART/
  한국 뉴스 소스를 독자 연동해야 하며, 이 부분은 TradingAgents에서 가져올 게 거의 없다.
- **출력 언어**: 한국어 리포트는 config로 즉시 가능(강점).

### 구독제 LLM 궁합
근본 불일치. TradingAgents = 다수의 병렬 종량 API 호출 전제. talon = Claude Max/Codex Pro 구독
단일 인증·호출수 제약. **오케스트레이션 아이디어는 빌리되 실행 엔진은 Claude Agent SDK로
전면 재구현**하고, 에이전트 수·라운드 수·툴 루프를 공격적으로 줄여 결정당 호출을 최소화해야 한다.

## 7. 결론

TradingAgents는 "LLM 멀티에이전트 리서치"의 잘 다듬어진 **참조 구현**이자, 동시에 talon이
**따라 하면 안 되는 비용/실행 안티패턴**의 교본이기도 하다. 가져올 것은 (1) 역할분담 토폴로지,
(2) 지연 반성 메모리 루프, (3) 결정론적 그라운딩·룩어헤드 규율 세 가지에 집중하고, LLM 레이어와
데이터·백테스트·집행 레이어는 전부 talon 제약(구독제·토스 API·한국시장·백테스팅 우선)에 맞춰
독자 설계하는 것이 옳다.
