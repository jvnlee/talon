# zipline-reloaded 정밀 분석 (talon 설계 관점)

- 레포: https://github.com/stefan-jansen/zipline-reloaded
- 라이선스: Apache-2.0 (개인·상업 사용 자유)
- 규모: Python ~54k LOC + Cython 성능 커널 (`_finance_ext`, rolling-window, adjustments)
- 정체성: Quantopian이 만든 이벤트 기반(event-driven) 백테스터. Quantopian 폐업(2020) 후 Stefan Jansen(*ML for Algorithmic Trading* 저자)이 유지보수하는 포크.
- 결론 한 줄: **전체 채택은 부적합, 설계 패턴과 일부 부품만 빌려온다(borrow).**

## 1. 아키텍처 / 모듈 구조

- `algorithm.py` (2,345줄): 유저 전략 API의 중심. `initialize/handle_data/before_trading_start` 콜백, `order*`, `set_slippage/commission`, `schedule_function`, `attach_pipeline/pipeline_output`.
- `gens/`: 시뮬레이션 클록(`sim_engine.pyx`)과 메인 제너레이터 루프(`tradesimulation.py`).
- `finance/`: 주문(`order`), 블로터(`blotter/`), 슬리피지(`slippage.py`), 수수료(`commission.py`), 리스크 컨트롤(`controls.py`), 원장(`ledger.py`), 성과지표(`metrics/`).
- `pipeline/`: 크로스섹션 팩터 엔진(퀀트 코어). `engine.py`, `term.py`, `expression.py`, `factors/`, `loaders/`, `domain.py`.
- `data/`: 데이터 포털, 번들(`bundles/`), bcolz/HDF5 바 리더, 조정(`adjustments.py`), FX.
- `assets/`: SQLite(SQLAlchemy/alembic) 기반 자산 마스터, `exchange_calendars` 연동.

## 2. 백테스팅 엔진 설계 — 이벤트 기반, 룩어헤드 방지

벡터화가 아니라 **이벤트 기반**이다. `sim_engine.pyx` 클록이 `SESSION_START → BEFORE_TRADING_START → BAR(들) → MINUTE_END → SESSION_END` 이벤트를 방출하고, `AlgorithmSimulator.transform()` 제너레이터가 이를 소비한다. 데이터 해상도는 **일봉/분봉**(틱·호가 없음)이다.

룩어헤드 방지의 핵심은 **주문 체결이 다음 바에서 일어난다**는 점이다. `every_bar()`는 바 시작 시 먼저 `blotter.get_transactions()`로 *직전 바에서 넣은* 주문을 현재 바 데이터로 체결한 뒤에야 `handle_data`를 호출한다. 즉 바 N에서 본 종가로 바 N에 체결할 수 없다 — 미래 정보 유출이 구조적으로 차단된다. 이것이 talon 백테스터가 반드시 지켜야 할 규율이다.

**체결/슬리피지 모델** (`SlippageModel.process_order(data, order)` 인터페이스):
- `VolumeShareSlippage`: 체결가 = `price*(1 ± price_impact*volume_share²)`, 바당 거래량의 2.5%(기본)까지만 체결 → 초과분은 다음 바로 이월(부분체결).
- `FixedSlippage`(고정 스프레드), `FixedBasisPointsSlippage`, `VolatilityVolumeShare`(ADV·변동성 기반 마켓임팩트, 20일 윈도우 캐시).
- 지정가는 `fill_price_worse_than_limit_price`로 체결 거부.

**수수료 모델** (`CommissionModel.calculate(order, transaction)`): `PerShare`(주당 0.1센트), `PerTrade`, `PerDollar`(15bp), 최소수수료 로직. 부분체결 시 누적 수수료를 정교하게 계산.

리스크는 `controls.py`의 `TradingControl`(MaxOrderCount/OrderSize/PositionSize, LongOnly, 제한종목)과 `AccountControl`(MaxLeverage/MinLeverage)로 주문 전 검증한다.

## 3. 퀀트 코어 — Pipeline 팩터 엔진 (가장 중요)

Pipeline은 **크로스섹션(날짜×종목 매트릭스) 팩터 계산 엔진**이다. 설계 흐름(`engine.py` 상단 주석에 명문화):

1. 도메인(달력·종목 유니버스) 결정 → 2. Term DAG 구성 + 룩백 윈도우별 추가 행 계산 → 3. `lifetimes` 매트릭스(날짜×종목의 거래가능 여부) → 4. 위상정렬 → 5. 순서대로 `_compute` 실행, **참조카운트로 워크스페이스 메모리 해제** → 6. 스크린 적용 후 narrow 포맷 출력. 대용량은 `run_chunked_pipeline`으로 날짜 청크 분할.

**팩터 표현 방식은 두 갈래**:
- 연산자 오버로딩 기반 표현식 엔진: `factor_a - factor_b`, `rank`, `zscore`, `demean`, 비교 → `NumericalExpression`으로 묶여 **numexpr**로 원소별 벡터 연산 컴파일. 즉 팩터 대수(algebra)를 선언적으로 조합.
- `CustomFactor`: `def compute(self, today, assets, out, *inputs)` — `inputs`는 `window_length×종목수` numpy 배열, `out`에 결과를 쓴다. 룩백 윈도우와 NaN(미상장 구간) 처리를 프레임워크가 관리. 내장 라이브러리: RSI, BollingerBands, MACD, Ichimoku, Aroon, SMA/EWMA/LWMA, VWAP, Returns, AnnualizedVolatility, MaxDrawdown 등.

**포인트인타임(PIT) 정합성** — 두 층위:
- 가격: `AdjustedArray`가 분할·배당·병합 조정을 **시뮬레이션 날짜 기준으로 지연 적용**한다. SQLite 조정 테이블(splits/mergers/dividends의 effective_date)을 각 날짜에 맞게 소급 → 미래 조정계수 유출 없음.
- 이벤트/펀더멘털: `loaders/events.py`, `earnings_estimates.py`가 `data_query_cutoff`(asof 타임스탬프)로 **발표 이전엔 안 보이게** 인덱싱. 실적발표·추정치의 룩어헤드를 원천 차단.

**ML 통합**: 네이티브 ML API는 없다. 대신 Pipeline이 팩터 매트릭스를 뽑아주고, ML4T 책의 패턴은 그 출력을 sklearn 등에 먹여 예측을 다시 `CustomFactor`로 되먹이는 방식이다. 즉 "팩터 → 피처 → 모델" 파이프라인의 앞단을 담당한다.

**멀티마켓**: `domain.py`에 `KR_EQUITIES = EquityCalendarDomain(SOUTH_KOREA, "XKRX")`가 **1급 도메인**으로 존재하고, `country.py`·`currency.py`·FX 리더로 한국+미국 동시 유니버스와 통화 환산을 추상화한다. talon의 한·미 동시 처리에 직접 참고 가치가 크다.

## 4. 데이터 파이프라인 & 유지보수

데이터는 "번들" ingest 방식: `quandl`(사실상 폐기), `csvdir`(사용자 CSV), bcolz(일·분봉)/HDF5 저장, 자산은 SQLite. 달력은 외부 `exchange_calendars`(XKRX 포함)에 위임. 토스 OpenAPI 같은 실시간 브로커 연동은 **전혀 없다** — 라이브 트레이딩 어댑터가 이 포크에서 제거됐다(과거 zipline-live/IB는 별도·방치 프로젝트). 순수 배치 백테스터다.

**유지보수 상태**: 활성이되 저속. v3.x, numpy 2·pandas 2 호환에 집중, 최근 커밋(2025-11)은 대부분 의존성 범프/CI. 1인 유지보수 + Quantopian 유래 대형 레거시라 신규 기능은 거의 없다. "죽지 않았지만 빠르게 진화하지도 않는다."

## 5. talon 적용성 평가

### 그대로 채택 (adopt): 없음
전체 프레임워크 이식은 과하다. Cython 빌드·무거운 의존성·US/Quandl 중심 데이터 번들·bcolz 레거시가 토스 API 기반 1인 에이전트에 부담이다.

### 패턴만 빌린다 (borrow) — 핵심
- **체결 시뮬레이터 인터페이스**: `SlippageModel.process_order` / `CommissionModel.calculate` 계약을 그대로 이식하고, 한국식 비용(증권거래세 0.18%, 매도 시 부과, 유관기관수수료)·미국식(거래소·SEC fee)을 각각 구현. 부분체결·거래량 상한 로직 재사용.
- **다음-바 체결 규율**: 백테스터의 룩어헤드 방지 제1원칙으로 채택.
- **PIT 데이터 계층**: 가격 조정을 시뮬 날짜 기준 지연 적용(AdjustedArray 개념)과 이벤트의 asof-cutoff. talon 데이터 정합성의 뼈대로 삼는다.
- **리스크 가드**: TradingControl/AccountControl 패턴(주문 전 한도 검증)을 talon 주문 파이프라인에 이식.
- **팩터 계산 규약**: `compute(today, assets, out, *inputs)` 시그니처(윈도우 배열→출력)를 talon 팩터 규약으로.
- **직접 재사용 가능 부품**: `exchange_calendars`(XKRX 한국 장·휴장일)와 domain/country/currency 멀티마켓 추상화.

### 참고만 (reference)
- Pipeline 엔진 아키텍처(Term DAG → 위상정렬 → 참조카운트 메모리 관리 → 청크 실행): talon이 종목 유니버스 스크리닝·팩터 랭킹을 배치로 돌릴 때 설계 참고.
- numexpr 기반 팩터 대수·랭킹/z-score/분위수 정규화 방식.

### 피할 것 (avoid)
- **데이터 번들 인프라 전체**(quandl 폐기, bcolz 레거시): 토스 OpenAPI 기반 자체 데이터 계층으로 대체.
- **단타(데이트레이딩) 정밀 시뮬레이션 용도**: 최소 해상도가 분봉이고 틱·호가·체결강도가 없어 한국 데이트레이딩의 정밀 체결 모델링에 근본적 한계. 스윙·중장기 백테스트엔 충분.
- **라이브/자동매매**: 어댑터 없음 → 페이퍼·실전은 별도 구축.

### 한국 시장 적용성
XKRX 도메인·달력이 이미 1급이라 스윙·중장기 백테스트 뼈대로는 유효하다. 관건은 **데이터**다: 한국 가격을 `csvdir`로 적재하고, 분할·배당 조정 테이블과 펀더멘털 PIT 데이터를 별도 확보해야 하며(공짜 소스 부재), 여기에 거래세·호가단위·상하한가·VI 같은 한국 특수 규칙은 직접 얹어야 한다.

### 구독제 LLM 제약과의 궁합
zipline은 LLM을 전혀 쓰지 않는 **결정론적 수치 엔진**이라 종량 과금 이슈와 완전히 무관하다. 오히려 이상적 분업 구도를 시사한다: 무겁고 반복적인 백테스트·팩터 계산은 LLM 밖 순수 코드로 결정론적으로 돌리고, 구독제 Claude Code/Agent SDK는 **전략 설계·리서치·팩터 코드 생성/수정·백테스트 결과 해석**만 담당한다. 팩터/시그널을 Pipeline 스타일 코드로 표현해두면 LLM이 읽고 고치기도 쉬워 토큰 효율이 좋다. 즉 zipline형 엔진을 talon 에이전트의 "도구"로 두는 구조가 구독제 제약과 잘 맞는다.
