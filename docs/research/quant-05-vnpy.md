# vnpy (VeighNa) 정밀 분석 — talon 설계 관점

- 레포: https://github.com/vnpy/vnpy · 버전 4.4.0 (2026-05) · 라이선스 MIT
- 분석 범위: `vnpy/event`, `vnpy/trader`, `vnpy/alpha`(4.0 신설 퀀트 코어) 소스 직접 정독
- 유지보수: 10년차, 매우 활발. 사모펀드·증권·선물사 실사용. GUI(PySide6)·게이트웨이·앱은 `vnpy_<name>` 규칙의 외부 플러그인으로 분리, 코어는 경량(alpha ~4,600줄 / trader ~4,300줄).

## 1. 전체 아키텍처

3계층 구조다. (1) **이벤트 엔진**(`event/engine.py`): `queue.Queue` + 데몬 스레드 1개가 이벤트를 꺼내 타입별 핸들러 리스트로 디스패치, 1초 타이머 이벤트 생성. 145줄로 극단적으로 단순하다. (2) **트레이더 계층**: `BaseGateway`(추상), `MainEngine`(OMS/오케스트레이터), VO 객체 모델(`TickData`/`BarData`/`OrderData`/`TradeData`/`PositionData`, `vt_symbol="symbol.EXCHANGE"` 네이밍 규칙), `BaseDatafeed`·`BaseDatabase` 추상. (3) **alpha 계층**: ML 다팩터 리서치/백테스트 파이프라인(코어 안에 신설).

주목할 설계 결정: **alpha 백테스팅 엔진은 라이브 게이트웨이/OMS와 완전히 분리**돼 있다. alpha 전략은 리서치 전용(오프라인 시그널 → 백테스트)이고 실거래 경로가 없다. 반면 전통 CTA 전략은 외부 `vnpy_ctastrategy`에서 백테스트·라이브 동일 코드로 돈다. talon에는 후자(동일 전략 코드가 페이퍼→실전 재사용) 철학이 맞다.

## 2. 데이터 파이프라인

심볼별 Parquet 파일 저장(`AlphaLab`, `daily/`·`minute/` 디렉토리). `load_bar_df`가 핵심 전처리를 수행한다: **첫 종가로 나눠 가격 정규화**(`close/close_0`), 거래정지일(전 컬럼 합=0)을 NaN 처리, `turnover/volume`로 VWAP 파생. 롤링 윈도우 워밍업을 위해 `extended_days`만큼 시작일 앞을 패딩한다. **지수 편입 종목 시점 관리**가 인상적이다: `shelve` DB에 날짜별 구성종목을 저장하고, `load_component_filters`가 각 종목의 연속 편입 구간 `(start, end)`을 복원해 그 구간 데이터만 남긴다 — 생존편향 방지의 실전적 구현이다.

## 3. 퀀트 코어 (가장 중요)

### 팩터/알파 표현 엔진
문자열 DSL을 **Python `eval()`**로 실행한다(`calculate_by_expression`). `DataProxy`가 사칙연산·비교 연산자를 오버로딩하고, `ts_*`(시계열)·`cs_*`(횡단면)·`ta_*`·`math_*` 함수들을 로컬 네임스페이스에 주입한 뒤 `eval(expression, {}, d)`로 평가. 예: `"ts_corr(close, ts_log(volume + 1), 20)"`. Qlib 유래 **Alpha158**, WorldQuant **Alpha101** 팩터셋이 전부 이 표현식 문자열로 정의돼 있다(`ts_delay`, `ts_slope`, `ts_rsquare`, `ts_resi` 등 회귀계 연산은 Polars 벡터화로 최적화). 모든 시계열 연산은 `.over("vt_symbol")`로 종목별 그룹핑 → **자연스럽게 시점 안전(point-in-time)**하다.

### 데이터 핸들러
`AlphaDataset`: `feature_expressions`(dict) + `label_expression` + train/valid/test 구간. `spawn` 멀티프로세스 풀로 표현식 병렬 계산. 핵심은 **infer용/learn용 프로세서 분리**다: `infer_processors`는 실거래에서도 쓸 변환(정규화)만, `learn_processors`는 학습 전용(라벨 결측 행 제거, 라벨 정규화 등 미래에 쓸 수 없는 변환). 라벨은 `"ts_delay(close, -3) / ts_delay(close, -1) - 1"`(T+1 종가 매수→T+3 종가 매도 수익률) — 음수 delay로 미래를 참조하되 **라벨에만** 국한.

### 전처리기 (누출 방지 관점)
`process_cs_norm`(횡단면 z/robust), `process_robust_zscore_norm`, `process_ts_norm`, `process_cs_rank_norm`, `process_replace_inf`. 정규화 함수 다수가 `fit_start_time`/`fit_end_time` 인자를 받아 **학습 구간에서만 통계를 적합**하도록 설계 — 누출 방지 의식이 있다(단, 일부는 기본값이 전체 구간이라 사용자가 명시해야 안전).

### ML 통합
`AlphaModel` 템플릿(`fit`/`predict`/`detail`)에 Lasso·**LightGBM**·MLP(PyTorch) 구현. 전부 로컬 학습, 클라우드/API 의존 0. `predict`는 `fetch_infer`(누출 없는 피처)로 예측, `fit`은 `fetch_learn` 사용. **alphalens**로 팩터 IC/분위 수익률 티어시트 분석.

### 워크플로우
`lab` 노트북 흐름: 구성종목 로드 → 바 로드 → `Alpha158` 데이터셋 → 프로세서 등록 → `prepare_data(filters)` → `process_data()` → 모델 `fit` → `predict`로 시그널 생성·저장 → **오프라인 시그널을 백테스트 엔진에 주입**.

## 4. 백테스팅 엔진

이벤트 기반 바 리플레이(`for dt in sorted(dts): new_bars(dt)`). 매 스텝 `cross_order()`(직전 주문 체결) → `on_bars()`(전략) → `update_daily_close()` 순서라 **주문은 다음 바에서 체결 = 룩어헤드 없음**. 체결 모델: 롱은 `order.price >= bar.low`면 체결, 체결가 `min(order.price, bar.open)`(보수적). **A주 상·하한가(±10%) 하드코딩** 가드 포함. 수수료는 `long_rate`/`short_rate` 비율. **일일 시가평가 손익**(보유손익+매매손익−수수료)을 종목·포트폴리오 2단계로 집계, Sharpe·MDD·수익회복비 산출. 벤치마크 대비 초과수익(alpha) 분석 별도 제공. 단, `show_performance`의 누적수익이 `pct_change().cum_sum()`(단순 합, 복리 아님)이라 근사치 — 그대로 복사하면 안 된다.

## 5. 리스크 관리 & 라이브 어댑터

코어의 리스크 관리는 **얕다**. 포지션 사이징은 전략 내부(`cash_ratio`, 균등가중, `top_k`/`n_drop`으로 회전율 통제, 최소보유일). 백테스트에 파산 체크만. 사전거래 리스크(주문 빈도·수량·미체결 한도)는 외부 `vnpy_riskmanager`. 라이브 어댑터는 `BaseGateway`: `connect`/`subscribe`/`send_order`/`cancel_order`/`query_account`/`query_position`/`query_history` + `on_tick`/`on_order`/`on_trade`/... 콜백이 이벤트를 push하는 깔끔한 패턴. 게이트웨이·데이터피드·DB 모두 `vnpy_<name>` 네이밍으로 동적 import되는 플러그인.

## 6. talon 적용성 평가

### 그대로 빌릴 것 (패턴)
- **팩터 표현식 개념 + Alpha158/Alpha101 라이브러리**: 표현식 문자열은 시장 중립적이라 KR/US 봉 데이터에 그대로 이식 가능. talon 퀀트 팩터 시드로 최고 가치.
- **AlphaDataset의 train/valid/test + infer/learn 프로세서 분리**, `fit_start/end`로 학습구간 한정 정규화 → 누출 방지 설계를 그대로 채택.
- **시점 관리**(종목별 `.over` 그룹핑, 편입구간 필터로 생존편향 제거), **일일 MTM 손익 회계**, **target-position + diff 실행** 패턴(목표비중 설정 → 차이만큼 주문).
- **BaseGateway 추상화**: 토스 OpenAPI 어댑터를 이 콜백 구조로 감싸면 백테스트↔라이브 전략 코드 재사용 가능.

### 참고만 (재구현)
- 이벤트 엔진: 단일 스레드 큐 패턴은 참고하되 talon 규모엔 asyncio 재구현이 자연스럽다.
- 백테스트 체결 모델(다음 바 체결, 보수적 체결가)의 룩어헤드 방지 순서는 반드시 참고.

### 피할 것
- **`eval()` 기반 표현식 엔진**: 보안·샌드박싱 불가. talon은 Polars 표현식 또는 파싱된 AST로 재구현할 것.
- **PySide6 GUI 전체**(talon은 텔레그램+CLI), 중국어 로그/로케일.
- **A주 상·하한가 ±10% 하드코딩** — 한국은 ±30%(2015~), 미국은 없음. 선물 `size`(계약승수) 중심 모델(주식은 size=1).
- **프레임워크 통째 채택**: 선물·A주 DNA, GUI 결합 생태계, `show_performance`의 비복리 근사 등. 무겁고 talon 목적과 안 맞다.

### 한국 시장 적용성
KRX(코스피/코스닥) 거래소 enum·게이트웨이·데이터피드가 **전무**. 미국은 IB 경유(NYSE/NASDAQ 지원). 토스 OpenAPI를 `BaseGateway`+`BaseDatafeed`+`BaseDatabase`로 직접 래핑해야 한다. 가격제한폭 ±30%, T+2 결제, 개인 공매도 제약(롱온리 초기엔 무관) 등은 talon 자체 로직으로. **팩터 표현식과 데이터셋/프로세서 계층은 시장 무관**하므로 즉시 재사용 가능한 것이 최대 이점. Parquet 일봉 저장 방식은 KR+US 동시 운용에 적합.

### 구독제 LLM 제약과의 궁합
**이상적**이다. vnpy 퀀트 코어는 LLM 의존이 0이고 API 종량 과금 요소가 전무하다. LightGBM/Lasso/MLP 학습·시그널 생성이 전부 로컬 결정론적 연산 → talon의 "구독제만" 제약과 완전 무충돌. 오히려 명확한 역할 분리를 시사한다: **퀀트 코어(vnpy식) = 무과금 로컬 시그널 생성**, **LLM(Claude Code 구독) = 그 시그널 위에 얹는 뉴스·시황·매매근거 서술/판단 계층**. 정량 신호는 vnpy 패턴으로, 정성 리서치는 LLM으로 깔끔히 분리하면 토큰 비용 없이 확장 가능하다.

## 결론
**verdict: borrow.** 프레임워크를 통째로 도입하기엔 무겁고 선물/A주/GUI 색채가 강하지만, 퀀트 코어의 설계 패턴(팩터 표현식 DSL, 데이터셋/프로세서 누출방지 구조, Qlib 팩터 라이브러리, 이벤트 기반 백테스터, 게이트웨이 추상화)은 talon 퀀트 레이어의 청사진으로 삼을 만한 최상급 참고자산이다.
