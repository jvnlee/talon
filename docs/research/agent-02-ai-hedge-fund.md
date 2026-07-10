# OSS 정밀 분석: virattt/ai-hedge-fund

- 분석일: 2026-07-09
- 대상: https://github.com/virattt/ai-hedge-fund (커밋 2026.7.3 기준)
- 규모/상태: 61k stars, 10.8k forks, MIT, 활발히 유지보수(마지막 push 2026-07-03). **명시적으로 "교육/PoC 전용, 실거래 아님"**
- talon 판정: **borrow** (핵심 설계 패턴은 차용, 코드/데이터/실행부는 대부분 부적합)

---

## 1. 저장소 전체 구조 — 두 개의 레이어

이 레포는 사실상 두 프로젝트가 한 저장소에 있다.

- **`src/` (v1, 실제 동작하는 제품)**: 유명 투자자 페르소나 기반 LLM 멀티에이전트. LangGraph로 오케스트레이션.
- **`v2/` (재작성 중, 대부분 미완성)**: 페르소나를 버리고 정량(quant) 파이프라인으로 재설계. README/VISION은 웅장하지만 **실제 구현은 20%**.

`VISION.md`/`ROADMAP.md`가 방향을 명확히 밝힌다: "펀드를 상시 가동되는 1급 객체로. 애널리스트는 pluggable alpha model. 백테스트=페이퍼=라이브가 동일 코드경로(`run_cycle`)." 이 철학은 talon 목표와 정확히 겹치지만, **오늘 코드로는 대부분 미구현**이다.

## 2. LLM 오케스트레이션 (핵심 심층 분석)

### 위상(topology)
`src/main.py`의 LangGraph `StateGraph`:
```
start_node → [선택된 N개 애널리스트 노드: 병렬 fan-out] → risk_management_agent → portfolio_manager → END
```
애널리스트 ~16종(Buffett, Munger, Graham, Ackman, Wood, Burry, Pabrai, Taleb, Lynch, Fisher, Jhunjhunwala, Druckenmiller, Damodaran) + 방법론 4종(valuation, sentiment, fundamentals, technicals).

### 역할 분담과 "토론/합의"의 실체
중요한 발견: **에이전트 간 토론·논쟁·합의·반성(reflection)·메모리가 전혀 없다.** 애널리스트들은 서로를 보지 못하고, 각자 독립적으로 `{signal: bullish/bearish/neutral, confidence 0-100, reasoning}`만 방출한다(단일 패스 fan-out). 유일한 "집계자"는 portfolio_manager인데, 그것도 각 애널리스트의 신호를 `{sig, conf}`로 압축해 받아 **LLM이 행동을 고르는** 방식이지 신호 블렌딩 수학이 아니다(그 블렌딩이 v2 portfolio construction인데 미구현).

### 프롬프트 설계 (talon이 훔칠 가치가 큼)
페르소나 에이전트(`warren_buffett.py`)의 패턴이 매우 규율 있다:
1. **결정론적 Python이 모든 무거운 분석을 수행** — ROE/부채/마진 스코어링, moat 분석, pricing power, 3단계 DCF 내재가치, margin of safety.
2. 그 결과를 **compact facts(JSON, `separators=(",",":")`)** 로 만들어 LLM에 전달. 원자료(raw)는 절대 안 보냄.
3. system 프롬프트에 페르소나 + 명시적 signal 규칙 + confidence 척도. reasoning은 "120자 이내"로 강제.
4. 출력은 pydantic + `json_mode` structured output. `call_llm`은 3회 재시도 + default_factory 폴백.

즉 **LLM은 판단·서술만, 계산은 코드가.** VISION의 원칙 "The LLM never touches the trade"가 코드로 관철된다.

### 리스크·포트폴리오 게이트 (결정론)
- `risk_management_agent`(순수 Python, LLM 없음): 연환산 변동성 → NAV 대비 포지션 한도(%), 종목 간 상관행렬로 상관 승수(0.70~1.10x)를 곱해 한도 축소, 현금/마진 캡 적용 → 종목별 `remaining_position_limit`(달러) 산출.
- `portfolio_manager`: 코드가 먼저 `compute_allowed_actions`로 각 종목의 허용 행동(buy/sell/short/cover)과 max 수량을 현금·마진·리스크 한도로 계산 → 순수 hold는 LLM에 안 보냄 → **LLM은 "허용된 행동 중 하나 + 한도 이하 수량"만** 고름. LLM이 한도를 초과할 수 없다.

### 비용 구조 (talon 제약과 직결)
- 1회 의사결정: `N_애널리스트 × N_종목` LLM 호출 + PM 1회. 전부 **종량 과금 API**(기본 OpenAI gpt-4.1; Anthropic/DeepSeek/Groq/Google/xAI/Moonshot/OpenRouter/Azure). 로컬 무료 경로는 **Ollama**뿐.
- **구독 인증(Claude Max/Agent SDK) 경로는 없음.** langchain provider + API 키 방식이 전제.

## 3. 백테스팅 엔진 — 두 개, 둘 다 약점

### v1 (`src/backtester.py` + `backtesting/engine.py`) — 이벤트 기반
- 영업일 루프를 돌며 **매일 전체 LLM 그래프를 재실행**(모든 애널리스트+리스크+PM). 체결은 그날 종가.
- 포트폴리오는 롱/숏 원가, 마진, 실현손익까지 제대로 추적. 지표: Sharpe, Sortino, MaxDD, 노출도, SPY 벤치마크.
- **치명적 약점 3가지:**
  1. **수수료·슬리피지·스프레드 모델 전무.** `TradeExecutor`는 원종가에 그냥 체결(`trader.py` 확인).
  2. **비용 폭발**: 1년 백테스트 = N종목 × ~250일 × (≈14 애널리스트 + 1 PM) LLM 호출 → 수만 회. 종량 API로도, 구독 rate limit으로도 비현실적. 이래서 "교육용"에 머무름.
  3. 캐시가 **인메모리 전용**(`data/cache.py`, 프로세스 종료 시 소멸) → 반복 백테스트마다 API 재호출.
- 룩어헤드: end_date 바운드 쿼리로 대체로 방지. 최근 커밋에서 metrics 룩어헤드 누수를 명시적으로 패치("query metrics by filing_date").

### v2 (`v2/backtesting/engine.py`) — 벡터라이즈 워크
- 종목별로 거래일 그리드를 걸으며 edge-trigger 진입, 고정 holding_days, equal-dollar 사이징, 포지션 중첩 금지. 체결은 종가.
- README는 "costs from day one"을 표방하지만 **실제로는 여전히 거래비용 0** — 비용 모델이 미구현 `pipeline/`에 있기 때문. 코드 주석도 "PEAD Sharpe 0.33 — not tradable yet"로 자인.

## 4. 전략 표현 방식 — 가장 훔칠 만한 아이디어

`v2/signals/base.py`의 **`AlphaModel` 추상화**가 이 레포의 최고 자산이다:
```
AlphaModel.predict(ticker, date, client) -> Signal(value ∈ [-1,+1], reasoning, metadata)
```
정량 신호(PEAD)와 LLM 투자자 에이전트가 **동일 인터페이스**를 구현한다. "view(확신도)"와 "position mechanics(타이밍/사이징/보유기간)"를 의도적으로 분리. `PEADModel`(어닝 서프라이즈 후 드리프트)이 구체 템플릿: point-in-time 필터(`filing_date <= as_of`), 8-K 우선 dedup, 45일 소급 필터까지 갖춘 실전형 예시.

`v2/event_study/`도 실제 동작: 시장모델 회귀로 CAR(누적초과수익) + 부트스트랩 CI + t검정. **신호 검증 프레임워크**로 유용.

## 5. 실제 구현 vs 문서 (냉정한 실태)
- **실동작**: `src/` 전체(v1), `v2/data`, `v2/event_study`, `v2/signals`(PEAD 1개만), `v2/backtesting`.
- **빈 docstring 스텁**: `v2/features`, `v2/validation`(CPCV/PBO), `v2/portfolio`(MV/Black-Litterman/risk-parity), `v2/risk`, `v2/pipeline`(Almgren-Chriss). README가 자랑하는 정량 스택의 핵심은 **아직 존재하지 않는다.**
- **라이브 트레이딩**: 없음. 브로커 어댑터 0. 웹앱(FastAPI+React+SQLite)은 flow 실행/저장만.
- **한국 시장**: 데이터 소스가 financialdatasets.ai(미국 중심) 단일. KRX 미지원. **talon KR 사이드엔 직접 도움 0.**

## 6. talon 적용성 평가

### 그대로 채택(adopt)
1. **`AlphaModel → Signal(value∈[-1,+1] + thesis)` 통일 인터페이스.** 정량 시그널·LLM 리서치를 같은 타입으로 흡수 → talon 신호 레이어의 뼈대.
2. **"LLM은 view만, 코드가 사이징·게이트" 분리 원칙.** 결정론적 리스크/주문 로직이 하드 게이트.
3. **결정론 리스크 레이어**: 변동성 기반 사이징 × 상관 축소 × NAV/현금 캡. 순수 Python, 그대로 이식 가능.
4. **compact-facts 프롬프트 규율**: 계산은 코드가, LLM엔 요약 fact만 JSON으로. 토큰·rate-limit 절약 → 구독제 필수 습관.
5. **DataClient Protocol 계약**: empty=진짜 없음 / 인프라 실패=RAISE(누락과 무신호를 구분). Toss 어댑터 계약으로 채택.

### 패턴만 차용(borrow)
- LangGraph fan-out→aggregate 위상.
- PM의 "코드가 허용행동 계산 → LLM은 제약 안에서만 선택" 패턴.
- event-study/CAR + PEAD를 talon 백테스트 **신호 검증** 도구로.

### 참고만(reference)
- v2 정량 스택 설계 문서(CPCV/PBO, Almgren-Chriss, Black-Litterman)는 **미구현이므로 코드가 아닌 체크리스트로** 참고.
- 투자자 페르소나 캐스트: 재밌지만 스타일화된 근사. 1인 단타에는 불필요.

### 피할 것(avoid)과 이유
- **백테스터를 그대로 쓰지 말 것**: 매일 LLM 그래프 재실행 → 구독제 LLM으로 완전 비현실적, 거래비용/슬리피지 0, 캐시 미영속.
- **financialdatasets.ai 채택 금지**: 한국 미지원.
- **종량 API LLM 경로 금지**: talon 제약 위반.

## 7. 한국 시장 + 구독제 LLM 궁합 (반드시 짚을 점)

- **한국 시장**: 이 레포에서 재사용 가능한 KR 자산은 0. Toss OpenAPI 어댑터를 `DataClient` Protocol 뒤에 새로 구현해야 함. 게다가 이 레포의 무게중심은 분기 재무제표·DCF 기반 **가치투자**인데, talon의 초점인 **단타/스윙**은 기술적·수급 신호 중심이라 페르소나 코어는 부적합. 이식되는 것은 코드가 아니라 **아키텍처**(신호 인터페이스, 리스크 게이트, PIT 백테스트).
- **구독제 LLM 궁합**: **정면 충돌.** 레포는 종량 API 전제 + 백테스트마다 LLM 재호출. 구독제(Claude Max/Agent SDK)에서 이 구조는 불가능. talon 원칙: (1) **백테스트 hot-loop에서 LLM 완전 배제** — 백테스트는 결정론 정량 시그널(v2 방식)만; LLM은 실시간 소수 의사결정의 리서치/서술에만. (2) LLM 호출을 Agent SDK 구독 인증으로 라우팅(langchain API-key 아님). (3) compact-facts 프롬프트로 rate-limit 내 유지. 요컨대 **이 레포의 "LLM=view, 코드=execution" 분리를 극단까지 밀어붙여야** 구독제에서 생존한다.

## 8. 한 줄 결론
설계 철학(alpha-model 통일 인터페이스, view/execution 분리, PIT 정직성, 결정론 리스크 게이트)은 talon의 청사진으로 삼을 가치가 크다. 그러나 **코드·데이터·백테스터·LLM 실행 경로는 talon 제약(한국시장·단타·구독제)과 대부분 맞지 않으므로 이식이 아니라 재구현이 전제**다.
