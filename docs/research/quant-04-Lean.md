# QuantConnect Lean 정밀 분석 (talon 설계 관점)

- 대상 레포: https://github.com/QuantConnect/Lean (Apache-2.0)
- 분석 커밋: `e709e62` (2026-07-08), 클론 크기 약 510MB, C#/.NET 8 모놀리식
- 유지보수 상태: 매우 활발(분석 시점 하루 전 커밋). 상용 SaaS(QuantConnect Cloud)의 오픈코어 엔진으로, 회사가 상시 개발.

## 1. 핵심 모듈 구조

Lean은 "이벤트 기반, 기관급" 백테스트/라이브 통합 엔진이다. 레포는 관심사별로 프로젝트가 분리된 대형 솔루션이다.

- `Common/` — 도메인 타입(`Data`, `Orders`, `Securities`, `Interfaces`). 엔진 전체의 계약(interface) 집합.
- `Engine/` — 실행 엔진. `AlgorithmManager`(메인 루프), `DataFeeds/`(구독·동기화), `TransactionHandlers/`, `Results/`, `RealTime/`, `Setup/`.
- `Algorithm/` + `Algorithm.Framework/` — 전략 API와 5단계 모듈형 프레임워크.
- `Indicators/` — 기술적 지표 **166개**(내장 팩터 원시재료).
- `Brokerages/` — 브로커 추상화 + Paper/Backtesting만 포함. **실제 라이브 어댑터(IB, Alpaca 등)는 별도 플러그인 레포**로 분리.
- `Optimizer/`, `Research/`(QuantBook/Jupyter), `ToolBox/`(데이터 변환기), `Report/`(성과 리포트).

핵심 통찰: **모든 확장점이 인터페이스**(`IFillModel`, `IFeeModel`, `ISlippageModel`, `IBrokerage`, `IDataQueueHandler`, `IAlphaModel` 등)로 노출되어 백테스트·페이퍼·라이브가 동일 코드로 돌아간다. talon이 반드시 흡수해야 할 설계 철학이다.

## 2. 백테스팅 엔진 설계 (이벤트 기반)

완전한 **이벤트 기반(event-driven)**, 벡터화 아님. `Engine/AlgorithmManager.cs`의 `Run()`이 `ISynchronizer.StreamData()`로부터 시간순 `TimeSlice`를 하나씩 당겨오며, 각 슬라이스마다 `algorithm.OnData(CurrentSlice)`를 호출하고 직후 `transactions.ProcessSynchronousEvents()`로 체결을 처리한다. 모든 심볼·데이터타입 구독이 단일 시간순 스트림으로 병합된다.

**룩어헤드 방지가 이 엔진의 백미다.** 모든 데이터는 `EndTime` 기준으로 타임스탬프되고, 슬라이스에는 `EndTime <= 현재 알고리즘 시각`인 데이터만 담긴다. 체결 모델(`Common/Orders/Fills/FillModel.cs`)의 거의 모든 메서드가 `if (pricesEndTime <= order.Time) return fill;` — 즉 **주문 제출 시각보다 오래된(stale) 데이터로는 체결하지 않는다**. 최신 커밋에는 `StalePriceTimeSpan`/`ShouldWaitForFreshDataOnStale` 로직이 추가되어, 한 봉 이상 지연된 fill-forward 가격으로 체결하려 하면 다음 신선 데이터를 기다린다. MarketOnOpen 주문은 **같은 봉에서 절대 체결되지 않고 다음 봉 시가**로만 체결된다. 동일 봉 체결로 인한 look-ahead를 구조적으로 차단하는 방식이며, talon 백테스터가 그대로 모방해야 할 규율이다.

**비용 모델은 심볼별 플러그형**이다.
- 체결: `EquityFillModel`, `ImmediateFillModel`, `LatestPriceFillModel` 등.
- 수수료: `Common/Orders/Fees/`에 30여 개 브로커별 모델(`InteractiveBrokersFeeModel`, `ZerodhaFeeModel`, `IndiaFeeModel` 등). **한국 브로커·거래세 모델은 없음.**
- 슬리피지: `VolumeShareSlippageModel`(체결비중² × priceImpact, 기본 0.025²×0.1), `MarketImpactSlippageModel`, `ConstantSlippageModel`.

## 3. 전략 표현 방식

두 스타일 공존.
1. **클래식**: `QCAlgorithm`을 상속해 `Initialize()`/`OnData()` 오버라이드(예제 808개 C# + 428개 Python).
2. **알고리즘 프레임워크**: 5단계 파이프라인 조합(`Algorithm.Framework/`).
   - Universe Selection → **Alpha(Insight 생성)** → Portfolio Construction(Insight → PortfolioTarget) → Risk Management → Execution(Target → 주문).

`IAlphaModel.Update(algorithm, slice)`는 `IEnumerable<Insight>`를 반환한다. **`Insight`**(`Common/Algorithm/Framework/Alphas/Insight.cs`)는 심볼에 대한 예측 객체로 `Direction(Up/Down/Flat)`, `Period`, `Magnitude`, `Confidence`, `Weight`, `SourceModel`을 담는다. 즉 **"신호 생성"과 "사이징·집행"을 완전히 분리**한다. `RsiAlphaModel`은 RSI가 30 하향/70 상향 돌파 시 `Insight.Price(symbol, period, Up/Down)`을 뱉을 뿐이고, 얼마를 살지·어떻게 집행할지는 하위 모델이 결정한다. 이 관심사 분리가 talon 설계에 가장 값진 패턴이다.

## 4. 퀀트 코어 상세

- **팩터/알파 정의**: qlib·WorldQuant식 **표현식(수식) 엔진은 없다**. 알파는 명령형 C#/Python 클래스로 작성한다. "팩터 원시재료"는 166개 지표이며 `CompositeIndicator`·`IndicatorExtensions`로 산술 합성(`rsi.Over(sma)` 등)한다. `RegisterIndicator` + `Consolidator`로 원 해상도(tick→second→minute→daily)를 자동 상향 집계해 지표에 주입한다.
- **데이터 핸들러**: `BaseData` 추상 클래스가 `GetSource()`(파일/URL 반환) + `Reader()`(한 줄→데이터 객체 파싱) 2메서드를 노출. **커스텀 데이터는 `BaseData` 상속만으로 결합**된다. LEAN 데이터 포맷은 심볼별·해상도별 파티션된 zip CSV.
- **포인트인타임(PIT) 데이터**: `FundamentalService`/`BaseFundamentalDataProvider.Get<T>(time, securityId, property)` — 특정 시점 기준으로 재무 값을 조회해 생존편향·재작성(restatement)편향을 회피. Coarse/Fine 펀더멘털 유니버스 선택 지원.
- **ML 통합**: 네이티브 ML 엔진 없음. `Research/QuantBook`(Jupyter)에서 Python(sklearn/torch/tf)으로 학습 → `IObjectStore`(키-값 blob 저장소, `Engine/Storage/LocalObjectStore.cs`)에 모델 직렬화 → 라이브 알고리즘에서 로드. 즉 **ML은 Python 생태계에 위임**.
- **포트폴리오 최적화**: 평균분산, Black-Litterman, 리스크 패리티, 최대 샤프, 최소분산 옵티마이저 내장(`Algorithm.Framework/Portfolio/`).
- **파라미터 최적화**: `Optimizer/`에 그리드/오일러 탐색, 클러스터링 분석.

## 5. 라이브 트레이딩 & 데이터 소스 연동

`IBrokerage`(`PlaceOrder/UpdateOrder/CancelOrder/Connect` + 주문상태·계좌 이벤트)와 `IDataQueueHandler`(`Subscribe`로 실시간 데이터 스트림) 두 인터페이스가 라이브 결합점. 구현체는 별도 레포로 분리되어 코어가 가볍게 유지된다. 인터페이스 기반이라 백테스트·페이퍼·라이브가 동일 전략 코드로 동작.

## 6. talon 적용성 평가

talon은 1인용·단일 포트폴리오·LLM 구독제·한국+미국·단타/스윙 에이전트다. Lean은 다중 자산군 대응 대형 .NET 모놀리식이며 LLM과 무관한 결정론적 퀀트 엔진이다. **직접 채택(코드 의존)은 부적합, 패턴 차용이 정답.**

### 그대로 채택 — 없음
.NET+Docker 런타임 의존, 510MB 규모, 다자산 복잡성은 1인 에이전트에 과설계다. 무엇보다 **한국 시장 미지원**: `Market.cs`에 KRX/KOSPI/KOSDAQ 없음(India만 비미국 선례로 존재), `market-hours-database.json`에 한국 거래시간 없음, 상하한가(±30%), 호가단위 테이블, 동시호가 단일가, 증권거래세·농특세, 원화, KRX/KOSDAQ 캘린더 전부 부재. 인도 사례처럼 커스텀 마켓+커스텀 브로커로 확장은 이론상 가능하나 데이터·모델링 부담이 매우 크다.

### 패턴만 차용 (핵심 권장)
1. **Insight 추상화**: LLM/퀀트 신호를 `{심볼, 방향, 기대기간, 강도, 신뢰도, 근거출처}` 타입 객체로 표준화. LLM의 "매매 근거"를 그대로 Insight로 사상 → 사이징·집행은 결정론적 코드가 담당.
2. **5단계 파이프라인**(Universe→Alpha→Portfolio→Risk→Execution)을 talon 내부 아키텍처로. **LLM은 Universe/Alpha 단계**(뉴스·섹터 리서치, 종목 스크리닝, 매매 근거)에 배치하고 **Risk/Execution은 결정론 코드**로 강제 → 구독제 LLM의 비결정성을 리스크 게이트로 격리.
3. **룩어헤드 방지 규율**: EndTime 스탬프 봉 + "`data.EndTime <= 주문시각`일 때만 체결" + MOO는 다음 봉 시가. talon 백테스터의 정직성을 담보하는 단일 최중요 교훈.
4. **심볼별 플러그형 비용 모델**: 한국(거래세 매도 약 0.18%, 유관기관 수수료, 브로커 수수료)·미국 각각의 `FeeModel`/`SlippageModel`을 인터페이스로 분리.
5. **PIT 데이터 제공자 인터페이스**: 재무·유니버스 데이터의 시점 정합성 확보로 생존편향 차단.
6. **`GetSource`+`Reader` 커스텀 데이터 패턴**: 토스증권 OpenAPI를 커스텀 데이터 소스로 래핑.
7. **인터페이스 기반 브로커 어댑터**: 토스 어댑터를 `IBrokerage` 유사 인터페이스 뒤에 두어 백테스트/페이퍼/라이브 패리티 확보.

### 참고만
166개 지표·포트폴리오 옵티마이저·수수료 상수는 이식하지 말고 Python `pandas-ta`/`TA-Lib`/`cvxpy`로 대체하되 **인터페이스 형태와 검증 로직만 참고**.

### 피할 것
- 코어 엔진 직접 의존(런타임·복잡도 과다, LLM 결합 이점 0).
- Lean에 한국 시장을 이식하려는 시도(선례 없는 데이터·모델링 대공사).
- Lean을 "AI 엔진"으로 기대하는 것 — LLM 커플링이 전무하다.

**LLM 구독제 궁합**: Lean은 종량 LLM API를 전혀 쓰지 않아 제약과 정면충돌은 없다. 그러나 반대로 LLM 활용 지점도 제공하지 않는다. talon에서 Lean은 (차용 시) **결정론적 백테스트/집행 기층**이고, LLM은 그 바깥의 리서치·근거 생성 층으로 분리하는 구도가 최적이다.

## 결론

verdict: **borrow**. 코드는 채택하지 않되 아키텍처(Insight 신호 추상화, 5단계 파이프라인, EndTime 기반 룩어헤드 방지, 플러그형 비용/데이터/브로커 인터페이스, PIT 데이터)를 talon의 백테스트·집행 설계 청사진으로 흡수한다.
