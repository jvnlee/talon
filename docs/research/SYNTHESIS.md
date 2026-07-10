# talon 설계 리서치 종합 (SYNTHESIS)

> 작성일: 2026-07-09
> 근거: `docs/research/` 내 23개 리포트(quant 01-10, agent 01-10, context 3종)
> 대상: 개인 1인용 KR+US 주식 단타/스윙 투자 에이전트
> 핵심 제약: (1) LLM은 구독제만(Claude Max 20x / Codex Pro 20x), 종량 API 절대 불가 (2) 브로커=토스증권 OpenAPI (3) 인터페이스=텔레그램+macOS PC (4) 초기 시드 1000만원 (5) 백테스팅→페이퍼→실전→자동매매의 단계적 진화, 초기엔 사람이 최종 판단·주문

---

## 0. 종합 결론 (3문장)

조사한 20개 레포 중 **프레임워크로 통째 채택할 대상은 없다**(유일한 `adopt`인 vectorbt조차 백테스트 검증 계층에 한정). talon은 **자체 경량 이벤트 드리븐 코어**를 만들되, 검증된 설계 패턴을 레포별로 이식해야 하며, 가장 반복적으로 확인된 대원칙은 **"결정론 코드(팩터·백테스트·체결·리스크 게이트) 하부 + 구독제 LLM(리서치·근거·최종판단 보조) 상부"의 2계층 분리**다. 최대 리스크 3가지는 **KR 분봉 장기 히스토리 공백**, **구독제 LLM 자동화 청구 정책의 불확실성**, 그리고 **거의 모든 LLM 트레이딩 레포가 저지른 안티패턴(백테스트 hot-loop에 LLM을 넣는 것)**이며, talon은 이 셋을 초기 설계에서 구조적으로 회피해야 한다.

---

## 1. talon 권장 아키텍처 컴포넌트 맵

각 컴포넌트별로 **차용할 레포/패턴**과 **직접 구축해야 하는 부분**을 명시한다.

### C1. 브로커/집행 어댑터 계층 (포트&어댑터)
- **역할**: 토스(실전 집행) ↔ KIS(모의·데이터·실시간) ↔ 백테스트 체결기를 동일 전략 코드로 스위칭.
- **차용**:
  - nautilus_trader의 포트&어댑터 `_template`(data.py/execution.py/providers.py/parsing) 골격 + reconciliation 개념
  - backtrader의 Store/Broker/Data 삼각 구조(어댑터 교체만으로 백테스트/페이퍼/실전 무변경 스위칭, 싱글턴 Store)
  - Lean의 인터페이스 결합점: `IBrokerage`(PlaceOrder/UpdateOrder/CancelOrder+이벤트), `IDataQueueHandler`(Subscribe)
  - vnpy의 `BaseGateway` 콜백 추상화(connect/subscribe/send_order/query_* + on_tick/on_order/on_trade push)
  - ai-hedge-fund의 `DataClient` Protocol 계약(empty=진짜 무데이터 / 인프라 실패=RAISE 구분)
- **직접 구축**: 토스 OpenAPI 어댑터(OAuth2 Client Credentials, `X-Tossinvest-Account` 헤더, 조건주문 OCO/OTO, 멱등키 `clientOrderId`, 미국 소수점 금액주문), KIS 모의투자 어댑터. **토스/KIS 네이티브 어댑터는 어느 레포에도 없음 → 전량 자작.**

### C2. 데이터 수집·저장 계층
- **역할**: KR/US 일봉·분봉·수급·공시·거시·뉴스 수집, 룩어헤드 안전한 PIT 저장.
- **차용**:
  - vectorbt의 `Data` 서브클래스 패턴(`download_symbol`/`update_symbol`만 구현하면 download/update/get/concat/스케줄갱신 상속) → `TossData`/`KISData`/`AlpacaData` 어댑터
  - vectorbt의 `DataUpdater` + asyncio `ScheduleManager`(장중 폴링·스케줄 갱신)
  - FinRL의 `DataProcessor` 파사드(download→clean→add_indicator→to_array)를 프로세서 설계 템플릿으로
  - qlib의 Point-in-Time 처리(`period_time`을 관측시점으로 붕괴, 미래 분기 참조 예외) → 스윙 펀더멘털 시그널
  - Lean의 커스텀 데이터 패턴(`BaseData.GetSource()`+`Reader()`), 포인트인타임 제공자 인터페이스
- **저장 포맷**: **SQLite/Parquet**(qlib `.bin` 배치 스토리지는 1인 감시종목엔 과설계, Toss 실시간 증분과 부정합 → 채택 금지).
- **데이터 소스 스택**(context-data-sources 확정):
  - 실거래/실시간: 토스 OpenAPI(KR+US, REST 폴링 ~1초, 캔들 1m/1d만·200개/요청)
  - KR 일봉/수급/펀더멘털: pykrx + FinanceDataReader(무료, 스크래핑, 이중화 필수)
  - **KR 분봉: 무료 정공법 부재 → 토스/KIS로 실시간 누적을 초기부터 가동(최우선 데이터 리스크)**
  - US 분봉: Alpaca 무료 Basic(10년+ 1분봉) = 정답
  - 공시: OpenDART(KR, 일 2만콜) / SEC EDGAR(US)
  - 거시: 한국은행 ECOS(KR) / FRED(US)
  - 뉴스: 네이버 검색 API + RSS(KR) / GDELT + Finnhub(US) — 요약·센티먼트는 구독제 LLM 위임(종량 회피)

### C3. 팩터/시그널 표현 엔진
- **역할**: 지표·시그널을 코드가 아닌 선언형 데이터로 정의, 백테스트/실시간 동일 코드 재사용.
- **차용**:
  - qlib의 표현식 팩터 DSL(문자열 수식→지연평가 AST: Expression/Feature/Rolling/Ref/Corr) — **개념만**
  - qlib의 룩어헤드 안전 윈도 부기(`get_extended_window_size`/`get_longest_back_rolling`) — **반드시 이식**
  - vnpy(Alpha)의 Qlib Alpha158 / WorldQuant Alpha101 팩터 라이브러리(표현식은 시장 무관, KR/US 봉에 그대로)
  - qlib `DataHandlerLP`의 learn/infer/raw 3키 + fit(train)/apply(test) 프로세서 분리(정규화 미래 누수 방지)
  - backtrader의 Lines 인덱스-0 시계열 추상화([0]=현재, [-1]=직전) + 단일코드 이중실행(next/once)
  - zipline의 `CustomFactor.compute(today, assets, out, *inputs)` 규약, Pipeline Term DAG(위상정렬·참조카운트 메모리 해제)
- **직접 구축**: **표현식은 반드시 `eval()`이 아니라 Polars expr 또는 파싱된 AST로 안전 재구현**(vnpy가 eval을 쓰는 것은 안티패턴). 초기엔 pandas/Polars 기반으로 시작, qlib 통째 종속은 회피.

### C4. 백테스팅 엔진 (→ 상세는 3장)
- **역할**: 이벤트 드리븐, 룩어헤드 차단, 현실적 체결/비용, KR/US 시장 특성 반영.
- **차용**: nautilus(시간축 규율·바 체결·Fill/Fee/Latency 3분할), zipline/backtrader/vnpy/Lean(다음 바 체결 규율), qlib(2차식 마켓임팩트·거래가능성 체크), vectorbt(벡터화 파라미터 스윕 엔진은 **adopt**).
- **직접 구축**: KR 상하한가 ±30%·호가단위·증권거래세·T+2·VI, US 소수점.

### C5. 체결/비용/슬리피지 모델
- **차용**:
  - nautilus의 `FillModel`(prob_fill_on_limit 큐위치·prob_slippage 1틱) / `FeeModel`(maker/taker·고정) / `LatencyModel` 3분할 독립 주입
  - qlib의 슬리피지=`impact_cost*(참여율)^2` + `min_cost` + 로트 라운딩 + 상하한/정지 배제
  - Lean의 `IFeeModel`/`ISlippageModel` 심볼별 플러그형, VolumeShareSlippage=체결비중²×priceImpact
  - vectorbt의 Order enums(size_type, fees 정률+정액, slippage 가격페널티, size_granularity 로트, reject_prob, sl/tp/trail)
  - zipline의 거래량 상한 초과분 부분체결
- **직접 구축**: **KR 매도 편측 거래세(~0.18%)** — vectorbt/qlib의 대칭 수수료 모델로는 표현 불가하므로 바별/편측 fees 배열 또는 사후보정.

### C6. 리스크/포지션 사이징 게이트 (결정론 하드 게이트)
- **역할**: LLM이 절대 건드릴 수 없는 사전거래 검증. 사람 최종판단 초기 단계에도 최상위 안전장치.
- **차용**:
  - nofx의 **"모델은 제안, 런타임은 처분"** — 포지션 한도·손실 컷·재진입 쿨다운·연속실패 세이프모드를 코드로 강제
  - nautilus의 `RiskEngine` 프리트레이드 게이트(max_notional_per_order, 주문 rate limit, 실패시 OrderDenied) + `PositionSizer`(entry/stop/equity 리스크% 사이징)
  - zipline의 `TradingControl`(MaxOrderSize/PositionSize/OrderCount, LongOnly, 제한종목) + `AccountControl`(MaxLeverage)
  - ai-hedge-fund의 `risk_manager.py`(연환산 변동성→NAV 대비 한도 × 종목간 상관 승수 × 현금/마진 캡, 순수 Python 이식 가능)
  - freqtrade의 Protections(StoplossGuard/MaxDrawdown/CooldownPeriod/LowProfitPairs) + PairLocks(연속손실 시 신규 진입 봉쇄)
  - Lean의 5단계 파이프라인 중 Risk/Execution은 결정론 코드로 강제
- **직접 구축**: 토스 `confirmHighValueOrder`(1억 이상)·일일 손실한도·과매매 방지 로직.

### C7. 성과/리스크 리포팅 계층
- **역할**: equity curve → 지표·티어시트 → 사람+LLM 공용 리뷰 표면.
- **차용**:
  - **quantstats 그대로 채택**(Sharpe/Sortino/Calmar/MDD/VaR/CVaR/Kelly/drawdown_details + HTML 티어시트). Apache-2.0, 벤더링 자유. yfinance 데이터 경로만 우회하고 자체 수익률 Series 주입.
  - vectorbt의 returns 모듈(Sharpe/Sortino/Calmar/Omega) + Deflated Sharpe Ratio(Bailey/López de Prado) 과최적화 방어
  - FinRL의 pyfolio/quantstats 티어시트, RD-Agent의 8차원 메트릭 벡터(IC/ICIR/RankIC/ARR/IR/-MDD/Sharpe)
- **직접 구축**: **트레이드 단위 승률·손익비**(quantstats는 기간 단위라 단타 부적합), 히스토리컬/블록 부트스트랩 VaR(quantstats 모수적 VaR·iid 몬테카를로는 팻테일 과소평가).

### C8. 검증/룩어헤드 방지 도구
- **차용**:
  - freqtrade의 바이어스 자동검출(`optimize/analysis/lookahead.py`, `recursive.py` — 진입/청산 캔들까지 잘라 재실행해 룩어헤드/재귀 바이어스 프로그램적 검출) — **필수 검증장치**
  - vectorbt의 walk-forward + labels 모듈(미래참조 학습라벨) + Deflated Sharpe
  - ai-hedge-fund의 event_study/CAR 프레임워크(시장모델 회귀+부트스트랩 CI+t검정), PIT 규율(filing_date<=as_of)
  - freqtrade/RD-Agent의 워크포워드 재학습(슬라이딩 윈도우 구간별 재학습→예측)
  - TradingAgents의 결정론 검증 스냅샷 그라운딩(`get_verified_market_snapshot` — 코드 ground-truth에 LLM 수치 주장 앵커링)

### C9. LLM 오케스트레이션 계층 (→ 상세는 5장)
- **역할**: 시황/섹터/뉴스 리서치, 지표·차트 해석, 매매 근거 제시. 집행은 절대 담당 안 함.
- **차용**:
  - ai-hedge-fund/Lean의 **Signal/Insight 통일 추상화**: `AlphaModel.predict(ticker,date)->Signal(value∈[-1,+1]+thesis)` 또는 Insight{심볼,방향,기대기간,강도,신뢰도,근거출처} — 정량 시그널과 LLM 리서치를 같은 타입으로 흡수
  - ai-hedge-fund의 **"LLM은 view/서술만, 결정론 코드가 사이징·주문·리스크 게이트"**(VISION: LLM never touches the trade)
  - nofx의 단일샷 구조화 결정 프롬프트(시스템=역할+하드제약+출력스키마, 유저=컨텍스트 → `<reasoning>`CoT + `<decision>`JSON) + 강건 파싱(유니코드 정제, ```json 펜스 추출, 스키마 검증) + 전량 감사 추적
  - StockAgent의 Secretary 검증기 패턴(LLM JSON 주문을 계좌상태·호가·상하한가에 대해 결정론 검증, 실패 시 오류 되먹여 자가교정 — LLM 출력 절대 맹신 금지)
  - TradingAgents/TradingAgents-CN의 다중 페르소나 토론(Bull/Bear, 리스크 3인) — **단, 5~8 에이전트를 소수로 압축**
  - 2단 모델 계층(deep_think/quick_think): 값싼 다수 작업은 Sonnet, 핵심 합의 판정만 Opus
- **직접 구축**: Claude Agent SDK 서브에이전트로 전면 재구현(모든 조사 레포의 LLM 계층은 langchain/OpenAI 종량 API 기반이라 코드 재사용 불가).

### C10. 메모리/학습 루프
- **차용**:
  - TradingAgents의 **지연 반성(deferred reflection)** + append-only 마크다운 메모리(pending→다음 run에 실현 알파 계산→2~4문장 반성→원자적 교체, 벡터DB 없이 저렴) — 페이퍼트레이딩 단계에 이상적
  - FinMem의 4계층(short/mid/long/reflection) 벡터 메모리 + **로컬 임베딩**(FAISS, 지수감쇠·중요도·리센시·피드백 강화) — 조회/스코어링 전부 로컬이라 LLM 무과금, 구독제와 궁합 우수
  - TradingAgents-CN의 ChromaDB 반성 메모리 개념(단, 자동 구동 루프를 talon이 완성)
  - nofx의 경량 메모리(최근 청산 N건 + 러닝통계 승률/PF/Sharpe/MaxDD를 프롬프트 주입)
- **직접 구축**: 반성 루프를 백테스트/페이퍼 **체결 결과**로 구동(FinMem/TradingAgents-CN이 미완성 방치한 부분). 임베딩은 로컬 모델(구독제와 무충돌).

### C11. 텔레그램 봇 + 스케줄러 하네스
- **차용**:
  - nofx의 NOFXi 텔레그램 운영비서(80% skill + 20% 동적 플래닝, 3계층 메모리 chatHistory/TaskState/ExecutionState)
  - ai-financial-agent의 태스크 분해를 진행상황 표시에 활용(분석 중: ...)
  - context-llm-harness: **macOS launchd**(StartCalendarInterval)로 headless `claude -p`를 개장/마감/장중 인터벌마다 호출, 상태를 파일/SQLite로 영속화(깨어남→상태 로드→행동→메모리 세이브→재수면)
  - 텔레그램은 **공식 Claude Code CLI를 서브프로세스로 호출**해 입출력만 중계(ToS 안전, OAuth 토큰 추출·재사용은 계정 정지 위험)
- **직접 구축**: 텔레그램 입출력 브리지, 잡 스케줄.

### C12. LLM 백엔드 추상화 (정책 헤지)
- **역할**: Claude ↔ Codex 스왑 가능. Anthropic 6/15 자동화 청구 정책이 언제든 종량화될 수 있으므로 필수.
- **차용**: context-llm-harness 권고(1차 Claude Code/Max 20x, Codex 폴백 준비, `ANTHROPIC_API_KEY` 세팅 금지=종량 전락, 단일 개인계정+공식 CLI 경로 고수).

---

## 2. 레포별 차용 요소 매트릭스

범례: **adopt**=코드/라이브러리 직접 사용 · **borrow**=설계 패턴 이식(코드 미포함) · **reference**=아이디어/체크리스트 참고 · **skip**=talon에 부적합

| 레포 | verdict | 코드 재사용도 | 핵심 차용 요소 | 라이선스 주의 |
|---|---|---|---|---|
| **vectorbt** | **adopt** | 라이브러리(백테스트 검증 계층) | Data 어댑터, 벡터화 파라미터 스윕, Order enums 체결모델, Deflated Sharpe, returns 모듈 | Apache-2.0 + Commons Clause(개인 사용 무문제) |
| **quantstats** | borrow→adopt | 라이브러리(리포팅 계층) | 성과·리스크 지표 50종, HTML 티어시트 | Apache-2.0(자유) |
| **qlib** | borrow | 패턴만 | 표현식 팩터 DSL, 룩어헤드 윈도 부기, PIT 재무, learn/infer 프로세서 분리, 2차식 마켓임팩트 | MIT |
| **nautilus_trader** | borrow | 패턴만 | ts_init 단일 시간축, 바 체결(OHLC 4점 분해), Fill/Fee/Latency 3분할, RiskEngine 게이트, CustomData 시그널 주입 | **LGPL-3.0**(파생 배포 시 검토) |
| **backtrader** | borrow | 패턴만 | Lines 인덱스-0, 단일코드 이중실행, minperiod 워밍업, Store/Broker/Data 삼각, Analyzer 플러그인 | **GPLv3**(코드 미포함 권장) |
| **Lean** | borrow | 패턴만 | Insight 추상화, 5단계 파이프라인(Universe→Alpha→Portfolio→Risk→Execution), IBrokerage/IDataQueueHandler, PIT 제공자 | Apache-2.0 |
| **vnpy** | borrow | 패턴만 | Alpha158/Alpha101 팩터 라이브러리, AlphaDataset 설계, target-position+diff 실행, 이벤트 백테스터 룩어헤드 순서 | MIT |
| **zipline-reloaded** | borrow | 부품+패턴 | 다음 바 체결, AdjustedArray(조정 지연 적용), TradingControl/AccountControl, **exchange_calendars(XKRX 한국 캘린더 직접 재사용)** | Apache-2.0 |
| **freqtrade** | borrow | 패턴만 | IStrategy 콜백 분리, 룩어헤드 자동검출 도구, 워크포워드, Protections/PairLocks, 리졸버 플러그인 | **GPLv3**(코드 미포함 권장) |
| **ai-hedge-fund** | borrow | 패턴만 | AlphaModel→Signal 통일 인터페이스, LLM never touches trade, risk_manager.py, compact-facts 프롬프트, event_study/CAR | MIT |
| **TradingAgents** | borrow | 패턴만 | 애널리스트→토론→심판 토폴로지, 지연 반성 메모리, 검증 스냅샷 그라운딩, 룩어헤드 규율 | Apache-2.0 |
| **nofx** | borrow | 패턴만 | 모델 제안/런타임 클램프 이중화, 단일샷 구조화 프롬프트, 결정 감사 추적, 텔레그램 운영비서, 토큰 예산 가드 | **AGPL-3.0**(코드 미포함) |
| **FinRobot** | borrow | 아이디어 | 리더-부하 오케스트레이션, "전문지식=지시주입 툴", 8섹션 리포트 목차 | Apache-2.0 |
| **TradingAgents-CN** | borrow | 아이디어 | 데이터소스 폴백 매니저, Quick/Deep 티어 분리, ChromaDB 반성 메모리 개념 | Apache-2.0 |
| **FinMem** | borrow | 아이디어 | 4계층 로컬 임베딩 메모리, 사후 reflection→메모리 저장 루프 | MIT |
| **Stockagent** | borrow | 단일 아이디어 | Secretary 검증기(generate-validate-repair) — **코드는 깨져있음, 포크 금지** | MIT |
| **FinRL** | reference | 아이디어 | DataProcessor 파사드, Turbulence 지수(레짐 리스크 신호), 매도선처리→매수 체결 순서 | MIT |
| **RD-Agent** | reference | 아이디어 | R&D 진화 루프, Thompson Sampling 밴딧(LLM 없이 실험방향 결정), Factor-from-report(PDF→팩터) | MIT |
| **FinGPT** | reference | 아이디어 | 애널리스트 출력 스키마, 감성 수치 팩터화(소스별 점수·정렬도) | MIT |
| **ai-financial-agent** | reference | 아이디어 | Zod 스키마 툴 정의 규약, 툴콜 dedup 캐시, 태스크 분해=진행표시 | (템플릿) |

**effective skip(참고조차 제한적)**: FinRL/FinGPT/Stockagent/ai-financial-agent의 **코드**, RD-Agent의 실행 인프라, 모든 레포의 **LLM 배관 코드**(종량 API), qlib `.bin` 스토리지·30종 딥러닝, FinRL RL 의사결정 코어, backtrader 데드 라이브 어댑터.

---

## 3. 백테스팅 엔진 설계 권고

talon은 **이벤트 드리븐 자체 엔진**을 만들되(vectorbt는 벡터화 파라미터 스윕 보조용으로 병행), 아래 3축을 반드시 갖춘다.

### 3.1 룩어헤드 방지 (제1원칙, 다수 레포 합의)
1. **단일 시간축 시퀀싱**: 모든 이벤트를 바 종가 시각(nautilus `ts_init`=close) 기준으로 정렬. "next-bar-open 체결 금지" 규율을 불변식으로.
2. **다음 바 체결**: 바 시작 시 직전 바 주문을 먼저 체결한 뒤 전략 로직 호출(zipline/backtrader/vnpy/Lean 공통). freqtrade식으로 백테스트 시그널을 `shift(1)`.
3. **워밍업 트림**: backtrader `minperiod` 전파(데이터·하위지표에서 계산) + qlib `get_extended_window_size`/`get_longest_back_rolling`로 필요한 과거 구간을 정확히 산정·프리페치. startup 캔들 트림.
4. **정규화 누수 방지**: qlib `DataHandlerLP`식 fit(train)/apply(test) 분리. 스케일러를 전체기간에 fit 금지(FinRL 안티패턴).
5. **PIT 정합**: 조정주가(분할·배당)를 시뮬레이션 날짜 기준 지연 적용(zipline AdjustedArray). 공시/실적은 `filing_date<=as_of`, 미래 분기 참조 예외(qlib P 연산자).
6. **자동 검출 장치**: freqtrade `lookahead.py`/`recursive.py`를 talon 필수 CI로 이식(진입/청산 캔들까지 잘라 재실행, 지표값 차이로 바이어스 탐지).

### 3.2 체결 모델 (3분할 독립 주입 — nautilus 골격)
- **FillModel**: prob_fill_on_limit(리밋 큐 위치), prob_slippage(1틱), random_seed 재현성. 바 백테스트는 OHLC를 4가격점(O→H→L→C)으로 분해, 거래량 배분, adaptive high/low ordering으로 한 바 내 TP/SL 선후 결정.
- **FeeModel**: 심볼별 플러그형(Lean IFeeModel). **KR 매도 편측 거래세 ~0.18%는 대칭 수수료 모델로 표현 불가 → 편측 fees 배열/사후보정**. US는 SEC fee + 소수점.
- **SlippageModel**: qlib `impact_cost*(참여율)^2` + min_cost, Lean VolumeShareSlippage=체결비중²×priceImpact, 거래량 상한 초과분 부분체결(zipline).
- **LatencyModel**: 주문 도착 지연(선택, 실전 근접 시).
- **오더북 불변 + 유동성 소진 추적**(nautilus): 역사 데이터 불변, 중복 체결 방지.
- **매도선처리→매수 체결 순서**(FinRL/vnpy): argsort로 현금 확보 후 매수.

### 3.3 KR/US 시장 특성 (전량 자작 — 어느 레포도 미지원)
- **KR**: 상하한가 ±30%(2015~, 중국 ±10%·A주 하드코딩과 다름), 호가단위(틱사이즈) 테이블, 증권거래세+농특세, T+2 결제, VI/동시호가 단일가, 거래정지(NaN close 배제), 1주 단위(qlib `trade_unit=100` 교체), KRX 캘린더(zipline `exchange_calendars`의 **XKRX 직접 재사용**).
- **US**: 소수점 매수(금액기반), SEC fee, XNYS/XNAS 캘린더.
- **멀티마켓 추상화**: zipline domain/country/currency로 KR+US 동시 유니버스 처리.

### 3.4 데이터 해상도 현실 (context 반영)
- **단타 정밀 체결**은 틱/호가가 필요하나 토스는 REST 폴링·1분봉만 → 초기 백테스트는 **분봉 스윙 중심**으로, 단타는 실시간 누적 데이터가 쌓인 뒤 검증. 틱 레벨 체결 시뮬은 nautilus L2/L3 개념 참고하되 데이터 확보가 선결.
- **벡터화 스윕**은 vectorbt(수천 파라미터 조합 동시), **정밀 이벤트 검증**은 자체 엔진 — 두 엔진의 결과 정합성 크로스체크.

### 3.5 과최적화 방어
- vectorbt Deflated Sharpe + walk-forward, RD-Agent 팩터 IC 중복제거(IC>0.99 폐기), In-sample 튜닝 금지(FinRobot 반면교사 — 같은 구간 반복 최적화 금지, train/test/OOS 분리).

---

## 4. 실시장 페이퍼 트레이딩 설계 권고

**토스는 모의투자 API가 없다**(치명적 공백). 두 경로를 병행한다.

### 4.1 하이브리드 브로커 구성 (context-toss 권고)
- **집행/에이전트 UX = 토스 OpenAPI**(실계좌, KR+US 통합, AI 친화, `llms.txt`).
- **페이퍼/데이터/실시간 = KIS Developers 보완계좌**(모의투자에서 미국 주문까지, WebSocket 실시간, 다중 분봉·과거데이터). 모의는 실전보다 호출한도 낮음.
- **브로커 추상화 레이어(C1)**로 KIS(모의) ↔ 토스(실전) 스위칭. 최종 실전 수렴 또는 토스 WebSocket 정식 출시 시 단일화 재평가.

### 4.2 자체 페이퍼 엔진 (토스 시세 위)
- 백테스트 체결기(C4/C5)를 **동일 코드로 실시간 시세에 연결**(nautilus 리서치-라이브 패리티, backtrader Store 스위칭, freqtrade dry-run 철학).
- 토스 REST 폴링(그룹별 토큰버킷, `X-RateLimit-*` 헤더 기반 백오프)로 시세 갱신, 가상 체결·가상 잔고 관리.
- **감사 추적 필수**(nofx): 시스템/유저 프롬프트·CoT·원문응답·실행로그·체결결과 전량 DB 저장. 페이퍼 검증 단계의 핵심.

### 4.3 학습 루프 (페이퍼→실전 준비)
- TradingAgents 지연 반성 + FinMem 로컬 임베딩 메모리로 **페이퍼 체결결과 기반 반성**을 축적(체결결과로 구동, 미완성 방치 금지).
- 수개월 페이퍼 후 성과지표(quantstats 티어시트 + 트레이드 단위 통계)로 실전 투입 판단.

### 4.4 라이브 정합성
- 컴포넌트 FSM + fail-fast(정밀도 불일치 즉시 에러, nautilus), reconciliation(주문·체결·잔고 상태 재조정), 결정론 단일 이벤트 루프로 백테스트-페이퍼-라이브 패리티 확보.

---

## 5. 구독제 LLM 제약 하의 오케스트레이션 권고

### 5.1 대원칙 (거의 모든 레포의 교훈)
1. **2계층 분리**: 결정론 코드(팩터·백테스트·체결·리스크 게이트) 하부 + 구독제 LLM(리서치·근거·판단 보조) 상부. **LLM은 시그널 생성만, 집행은 결정론 코드**(ai-hedge-fund VISION, nofx, StockAgent secretary).
2. **백테스트 hot-loop에서 LLM 완전 배제**(최중요 안티패턴 회피): ai-hedge-fund/TradingAgents/RD-Agent/StockAgent가 영업일마다 전체 LLM 그래프 재실행(연간 수만 콜) → 구독 rate limit·종량 양쪽에서 비현실적. talon 백테스트는 결정론 정량 시그널만.
3. **결정당 호출 1자릿수로 압축**: TradingAgents 16~20콜, TradingAgents-CN 12~20콜은 구독제에서 감당 불가. nofx식 단일샷 구조화 결정 프롬프트 지향.
4. **계산은 코드, 서술만 LLM**: compact-facts(ai-hedge-fund) — 지표·수치는 전부 Python이 계산해 요약 fact JSON만 LLM에 전달, reasoning 글자수 제한. 검증 스냅샷 그라운딩(TradingAgents)으로 환각 억제.

### 5.2 사용량 예산 (context-llm-harness 실측 필요)
- **Max 20x 한도**: 5시간 롤링 창(2026/5/6 영구 2배, 추정 ~200-800+ 프롬프트/~22만 토큰) + 주간 상한 2종(전체 1개 + Sonnet 전용 1개). Max 기준 **주 ~480 Sonnet시간 / ~40 Opus시간**급 → **Opus가 병목**.
- **라우팅 전략**: Sonnet 위주(다수 저비용 리서치), Opus는 핵심 합의 판정만 선별(Quick/Deep 2티어). Sonnet 라우팅 시 "장중 수 회 + 정기 브리핑"은 Max 20x 한도 내 충분.
- **자체 예산 관리**: 실행당/주간 토큰 자체 카운팅 + 백프레셔 로직(nofx `EstimateTokens` vs 컨텍스트 한도 가드, 초과 시 호출 차단/유니버스 축소). Max 20x 정확 수치는 비공개 → **자체 로깅으로 캘리브레이션**.
- **메모리로 컨텍스트 절약**: FinMem 로컬 임베딩(무과금)으로 관련 메모리만 조회 주입, 메모리 점프/정리로 컨텍스트 상한 능동 관리.

### 5.3 실행 하네스 (ToS 준수)
- **인증**: 공식 Claude Code CLI 로그인 세션으로 구독 한도 사용. **`ANTHROPIC_API_KEY` 세팅 금지**(종량 API 전락). OAuth 토큰 추출·서드파티 재사용 금지(계정 정지 사례).
- **스케줄**: macOS launchd로 headless `claude -p`를 개장/마감/장중 인터벌 호출. 상태 파일/SQLite 영속.
- **텔레그램**: 공식 CLI 서브프로세스 호출로 입출력 중계(커스텀 클라이언트 아님).
- **백엔드 추상화(C12)**: Anthropic 6/15 자동화 청구 정책이 보류 후 재설계 중 → 언제든 종량화 가능. Claude↔Codex 스왑 가능하게 설계, 정책 재발표 모니터링.

### 5.4 역할 그래프 (압축된 토폴로지)
- TradingAgents/ai-hedge-fund 토폴로지를 참고하되 **소수 서브에이전트로 압축**: 예) 리서처(시황·뉴스·수급) + 강세/약세 반대근거(확증편향 완화) + 판정(Research/Portfolio Manager). Claude Agent SDK 서브에이전트로 재구현.
- 출력은 Signal/Insight 통일 스키마(value∈[-1,+1]+thesis+기대기간+신뢰도+근거출처)로 강제(pydantic/structured output + graceful fallback).
- 토론 종료는 턴 카운터가 아니라 **근거 품질 기반 종료조건** 설계(TradingAgents-CN 반면교사).

---

## 6. 피해야 할 안티패턴

각 안티패턴은 **어느 레포에서 관찰됐는지** 명시한다.

| # | 안티패턴 | 출처(반면교사) | talon 대응 |
|---|---|---|---|
| A1 | **백테스트 hot-loop에 LLM 투입**(영업일마다 그래프 재실행=수만 콜) | ai-hedge-fund, TradingAgents, RD-Agent, StockAgent | 백테스트는 결정론 정량 시그널만, LLM 완전 배제 |
| A2 | **거래비용/슬리피지/거래세 0 백테스트** | FinRL, FinRobot, ai-hedge-fund, TradingAgents-CN | 3분할 체결모델 + KR 편측 거래세 필수 |
| A3 | **인샘플 과적합**(같은 구간에서 개발+튜닝, Buy&Hold 이길 때까지) | FinRobot | train/test/OOS 분리, LLM엔 가설제안만·테스트구간 튜닝 금지 |
| A4 | **팩터 표현식을 `eval()`로 실행** | vnpy(alpha) | Polars expr/파싱 AST로 안전 재구현 |
| A5 | **RL/신경망 정책을 의사결정 코어로**(설명 불가, 과적합) | FinRL | 설명가능한 근거 제시 요구와 충돌, 채택 금지 |
| A6 | **정규화를 train/test 분할 전 전체기간에 fit**(누수) | FinRL | fit(train)/apply(test) 분리(qlib DataHandlerLP) |
| A7 | **LLM 자기평가 confidence를 실자금 리스크 근거로**(미캘리브레이션) | TradingAgents-CN | 리스크는 결정론 코드가 계산, LLM confidence는 보조 |
| A8 | **LLM을 매매 실행 경로에 직접**(재현·감사 불가) | StockAgent, 대부분 agent 레포 | 시그널 생성(LLM)과 집행(코드) 반드시 분리 |
| A9 | **기간 단위 승률·손익비를 단타 평가에** | quantstats | 트레이드 단위 통계 자체 구현 |
| A10 | **모수적 VaR/iid 몬테카를로**(팻테일 과소평가) | quantstats | 히스토리컬/블록 부트스트랩 병행 |
| A11 | **`ANTHROPIC_API_KEY` 세팅 / OAuth 토큰 추출 재사용** | context-llm-harness | 공식 CLI 로그인 세션만, 종량·계정정지 회피 |
| A12 | **단일 무료 데이터 API 의존**(무료 티어 축소 트렌드) | context-data-sources | 소스 이중화 + 정합성 크로스체크 |
| A13 | **KR 분봉 실시간 누적 미가동**(과거는 못 삼) | context-data-sources | 초기부터 자체 DB 적재 파이프라인 가동(최우선) |
| A14 | **train 모드가 미래 수익으로 메모리 warmup**(구조적 룩어헤드) | FinMem | 메모리 학습에 미래참조 절대 금지 |
| A15 | **창발형 ABM/시뮬레이터를 백테스트로 오해**(실 시세 무관) | StockAgent | 실 KRX/US 시세 이벤트드리븐 엔진으로 검증 |
| A16 | **토론 종료=단순 턴 카운터**(수렴 판정 아님) | TradingAgents-CN | 근거 품질 기반 종료조건 |
| A17 | **GPLv3/AGPL/LGPL 코드 직접 포함**(카피레프트 전파) | backtrader, freqtrade, nofx, nautilus | 패턴만 차용, 원코드 미포함 |
| A18 | **qlib `.bin` 배치 스토리지·30종 딥러닝 파이프라인** | qlib | SQLite/Parquet + LightGBM 수준으로 충분 |
| A19 | **메타클래스 과용 아키텍처 모방**(가독성·타입안정성 저하) | backtrader | 명시적·타입힌트 가능한 구조 |
| A20 | **반성 루프를 설계만 하고 미완성 방치** | TradingAgents-CN, FinMem | 체결결과로 실제 구동 완성 |

---

## 7. 남은 열린 질문들

### 7.1 브로커/데이터 (즉시 검증 필요)
1. 토스 그룹별 **정확한 rate limit 수치**(현재 응답 헤더로만 동적, 예시 초당 10 burst) — KR 분봉 폴링·다종목 감시 설계 변수.
2. 토스 **WebSocket 정식 출시 시점**(2026 하반기 "완성형" 목표) 및 **모의투자/뉴스 API 추가 여부** — 하이브리드 단일화 재평가 트리거.
3. 토스 candles의 **분봉 과거 룩백 한도**(공식 명세 직접 확인) — KR 분봉 누적 전략의 설계 변수.
4. 토스 **자동매매 약관 전문**(앱 내 동의)의 명시적 봇 제한 조항 유무 최종 확인.
5. KIS/LS **모의투자의 해외(미국) 실시간 시세·주문 세부 제약**(모의는 실전 대비 기능·한도 축소 가능).
6. KRX Data Marketplace/코스콤 **장기 KR 분·틱 데이터 실제 가격·라이선스**(유료 확보 시).
7. Alpaca 무료 IEX 피드 **커버리지가 백테스트 신뢰도에 충분한지**, 전체 SIP 유료 전환 기준.
8. 네이버 검색 API **본문 부재 보완**(개별 크롤 vs 유료)과 저작권 허용 범위.

### 7.2 LLM 구독제 (정책 리스크)
9. Anthropic이 보류한 **6/15 자동화 크레딧/종량 정책 재발표 시점·형태** — talon 경제성 최대 변수.
10. Max 20x **정확한 5시간/주간 수치**(비공개) — 자체 로깅 캘리브레이션 필요.
11. **Agent SDK 구독 인증 최신 절차**(`claude setup-token` vs CLI 로그인 세션) 및 launchd 헤드리스 환경 **토큰 자동 갱신 지속성** 실검증.
12. Codex 구독으로 `codex exec` 상시 자동화 시 **OpenAI 계정 리스크 발생 규모**(공개 사례 부족).
13. 실거래 확장으로 "업무성 상시 자동화"가 되면 각사 약관 정공법은 **Commercial+API 키(종량)** → "구독만" 제약과 충돌, 그 시점 비용/약관 재평가.

### 7.3 아키텍처 결정 (초기 방향)
14. **qlib 실제 채택 여부 vs pandas/Polars 기반 자체 팩터·백테스트** — KR 어댑터 자작 비용 감안. (권고: 초기 pandas/Polars, 검증 후 US 우선 qlib 도입 신중히.)
15. 단타 정밀 체결 시뮬을 위한 **틱/호가 데이터 확보 경로**(토스 REST·1분봉 한계) — 확보 전엔 분봉 스윙 중심.
16. 반성/메모리 루프의 **로컬 임베딩 모델 선정**(macOS, GPU 없음) 및 성능·비용 검증.
