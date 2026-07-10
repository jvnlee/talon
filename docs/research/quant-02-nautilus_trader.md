# nautilus_trader 정밀 분석 (talon 설계 관점)

- 레포: https://github.com/nautechsystems/nautilus_trader
- 라이선스: LGPL-3.0 / 상업 주체(Nautech Systems) 후원
- 분석 시점 기준: v1.231.0 Beta, v2.0.0rc1 (Cython v1 → Rust/PyO3 v2 전환 중)
- 유지보수: 매우 활발 (분석일 당일에도 커밋 존재), 대규모 코드베이스(207MB)

## 1. 한 줄 정의

nautilus_trader는 **팩터/알파 리서치 플랫폼이 아니라, "리서치-라이브 패리티"를 목표로 한 프로덕션급 이벤트 드리븐 실행/백테스트 엔진**이다. Rust 네이티브 코어가 결정론적 이벤트 런타임을 제공하고, Python은 전략·설정·오케스트레이션을 담당하는 "컨트롤 플레인"으로만 쓰인다. 동일 전략 코드가 백테스트·샌드박스·라이브에서 재컴파일 없이 동작한다.

## 2. 아키텍처

- **패러다임**: DDD + 이벤트 드리븐 + 포트&어댑터(헥사고날) + crash-only + fail-fast. 품질 우선순위는 신뢰성 > 성능 > 모듈성 > 테스트성.
- **단일 스레드 커널**: `NautilusKernel`이 `MessageBus`(pub/sub·req/rep·command/event), `Cache`(인메모리 상태), `DataEngine`, `ExecutionEngine`, `RiskEngine`, `Portfolio`를 단일 스레드에서 순차 처리. LMAX Disruptor식 결정론 확보. 네트워크 I/O·영속화·어댑터만 별도 스레드/async(tokio)로 돌고 채널로 커널에 이벤트 전달.
- **데이터 흐름 (호가 tick 예)**: 어댑터가 원시 WS 메시지 → `QuoteTick` 파싱 → MPSC 채널 → `DataEngine.process_data` → `Cache.add_quote`(먼저 캐시) → `MessageBus.publish`(토픽 `data.quotes.VENUE.SYMBOL`) → 구독 전략 `on_quote_tick`. 캐시-후-발행 순서라 핸들러에서 항상 최신값 조회 가능.
- **실행 흐름 (주문)**: 전략 `submit_order` → `RiskEngine` 프리트레이드 검증(실패 시 `OrderDenied`, 거래소 미도달) → `ExecutionEngine` 라우팅 → `ExecutionClient` 어댑터 → 거래소. 체결/승인 이벤트가 역방향으로 흘러 캐시·포지션·포트폴리오 갱신 후 전략 핸들러 호출.
- **컴포넌트 FSM**: 모든 컴포넌트가 PRE_INITIALIZED→READY→RUNNING→STOPPED→DISPOSED 등 유한상태기계로 관리. `panic=abort` 릴리스 빌드로 불변식 위반 시 즉시 종료 후 재기동(crash-only).

## 3. 백테스트 엔진 (핵심)

- **이벤트 드리븐, 나노초 해상도**(벡터화 아님). 여러 venue·instrument·전략 동시 백테스트.
- **룩어헤드 방지의 정석**: 모든 이벤트를 `ts_init` 기준으로 시퀀싱. 바(bar)의 `ts_init`은 **반드시 종가 시각(close)** 이어야 하며(`ts_init_delta`로 보정), open 타임스탬프 데이터를 그대로 쓰면 룩어헤드 발생. "next-bar-open" 네이티브 모드를 **의도적으로 제공하지 않음**(이전 바 신호로 당일 시가 체결 = 룩어헤드라는 이유). 이 규율은 talon이 그대로 흡수할 원칙.
- **바 실행 모델**: 바만 있어도 내부적으로 top-level 오더북 시뮬레이션 유지. OHLC를 4개 가격점(O→H→L→C)으로 분해, 거래량 25%씩 배분(잔량은 종가에). `bar_adaptive_high_low_ordering=True`면 시가가 고가/저가 중 어디에 가까운지로 경로 추정(고정 50% → 적응 75~85% 정확도). 한 바 안에서 TP/SL이 모두 걸릴 때 어느 쪽이 먼저 체결되는지를 결정.
- **체결가/매칭**: L2/L3는 실제 호가창을 walk(taker 부분체결·가격충격 자연 반영). L1(quote/trade/bar)은 단일 레벨 북, 잔량은 1틱 슬리피지. 스톱마켓은 바 데이터에서 갭이면 시가 체결, 정상 관통이면 트리거가 체결(슬리피지 상한).
- **Fill Model(체결 확률 모델)**: `prob_fill_on_limit`(리밋 큐 위치 확률), `prob_slippage`(L1 한정 1틱 슬리피지 확률). 여기에 `ThreeTierFillModel`, `SizeAwareFillModel`, `VolumeSensitiveFillModel`, `MarketHoursFillModel` 등 합성 오더북 생성 서브클래스로 유동성/시장충격 시뮬레이션. `random_seed`로 재현성 확보.
- **오더북 불변성 철학**: 역사적 데이터는 절대 변경하지 않음. `liquidity_consumption=True`면 레벨별 소진량 추적으로 중복 체결 방지, 신규 데이터 도착 시 리셋. trade tick을 유동성 증거로 활용, 트리거 trade의 소진량 pre-seeding으로 이중 체결 방지.
- **수수료/슬리피지/지연 모델 분리**: `FeeModel`(maker/taker %, 고정 수수료, per-order/per-fill 선택), `LatencyModel`(주문 도착 지연 나노초), `FillModel`을 각각 독립 주입. 이 3분할은 talon 백테스터가 반드시 흉내낼 구조.
- **정밀도 불변식**: 모든 가격/수량이 instrument의 `price_precision`/`size_precision`과 불일치 시 즉시 `RuntimeError`. 조용한 데이터 손상 원천 차단.

## 4. 전략 표현 방식

- **명령형 클래스 기반**. `Strategy`(← `Actor`) 상속, `on_start`/`on_bar`/`on_quote_tick`/`on_order_filled`/`on_position_opened` 등 `on_*` 핸들러 구현. 설정은 frozen `StrategyConfig`(msgspec) 분리.
- **표현식/DSL 엔진 없음**. qlib류의 알파 표현식 파서·팩터 그래프가 전혀 없다. 지표는 `register_indicator_for_bars()`로 등록해 자동 갱신. 지표 라이브러리(momentum/trend/volatility/volume/이동평균/fuzzy candlesticks)는 Cython 최적화.
- **커스텀 신호 주입점**: `publish_signal`/`subscribe_signal`(경량 str/int/float), Actor 기반 `CustomData`(Arrow/Parquet 직렬화, `ts_init` 정렬로 시점 정합 replay). **외부 리서치·LLM 파생 시그널을 백테스트에 룩어헤드 없이 주입하는 정확한 후크가 이것**이다.

## 5. "퀀트 코어"의 실체 — 중요

임무가 요구한 팩터/알파 표현식 엔진, 데이터 핸들러 기반 ML 통합, 포인트인타임 팩터 스토어는 **nautilus에 존재하지 않는다**. 확인 결과:

- `sklearn/torch/xgboost/lightgbm` 등 ML 의존성이 코어에 전무. ML은 순수 DIY(전략 안에서 모델 로드 후 `on_bar`에서 호출).
- 알파/팩터 개념 없음. 대신 제공하는 것은 (a) 결정론적 이벤트 시뮬레이션, (b) 지표 라이브러리, (c) Parquet+DataFusion(Rust) 데이터 카탈로그, (d) 플러그인형 `PortfolioStatistic`(Sharpe/Sortino/승률 등)+tearsheet, (e) 옵션 `greeks` 모듈.
- 즉 nautilus는 **"팩터를 만드는" 도구가 아니라 "팩터/시그널을 실행·검증하는" 인프라**다. talon의 퀀트 팩터 리서치는 별도 레이어(qlib 등)에서 하고, 그 산출 시그널을 CustomData로 주입하는 그림이 정석.

## 6. 라이브 어댑터 / 데이터 소스

- 포트&어댑터로 crypto CEX/DEX 다수, Interactive Brokers(FX·주식·선물·옵션 멀티 venue), Databento·Tardis(데이터), Betfair 등. 어댑터는 `data.py`/`execution.py`/`providers.py`/`parsing/`로 구조화.
- 라이브 상태 재조정(reconciliation), Redis 상태 영속화, crash-only 재기동.
- **한국 시장/토스증권 어댑터 없음**. IB 파싱에 한국 거래소 코드 일부만 존재. 토스 OpenAPI 어댑터는 `_template` 어댑터를 참고해 직접 구현해야 하며 WS/REST 정규화·instrument provider·reconciliation까지 상당한 작업량.

## 7. talon 적용성 평가

### 그대로 채택 (Adopt) — 없음
전체 도입은 부적합. 이유: (1) HFT·멀티venue·나노초 지향의 프로덕션 엔진으로 1000만원 1인·인간개입·스윙/단타 스코프에 과설계, (2) 한국 시장 브로커 부재로 코어 가치의 절반이 미충족, (3) Rust 툴체인+대규모 코드+v1→v2 파괴적 전환기(RC, breaking change 진행)로 유지보수 리스크, (4) 러닝커브 과다.

### 패턴만 빌림 (Borrow) — 강력 권장
- **`ts_init`(=바 종가) 단일 시간축 시퀀싱**과 "next-bar-open 금지" 룩어헤드 규율.
- **OHLC→4가격점 + 적응형 H/L 순서** 바 체결 로직(단타/스윙 백테스트 정확도 핵심).
- **FillModel / FeeModel / LatencyModel 3분할** 주입 구조.
- **오더북 불변 + 유동성 소진 추적** 철학(보수적 체결 시뮬).
- **RiskEngine 프리트레이드 게이트**(max_notional, 주문 rate limit, OrderDenied)와 **PositionSizer**(entry/stop/equity 기반 리스크 사이징).
- **CustomData/Signal 시점정합 주입점** — talon의 LLM 리서치·뉴스·팩터 시그널을 백테스트/라이브에 동일 인터페이스로 흘려보내는 설계.
- **리서치-라이브 패리티**(동일 전략 코드) 원칙과 결정론적 단일 스레드 이벤트 루프.

### 참고만 (Reference)
- `docs/concepts/backtesting/`(fill-models, fill-prices-and-matching, bar-execution)은 체결 시뮬레이션 설계 교과서. talon 백테스터 스펙 작성 시 직접 참조 가치 높음.
- Parquet+Arrow 데이터 카탈로그 스키마, tearsheet/PortfolioStatistic 플러그인 패턴.

### 피할 것 (Avoid)
- 코드 통째 의존/포크. LGPL-3.0(동적 링크 시 사용 가능하나 파생·배포 시 제약), v2 전환기 불안정, Rust 빌드 부담.
- nautilus를 팩터 리서치 도구로 기대하는 것(그 기능 없음).

## 8. LLM 구독제 제약과의 궁합

nautilus는 LLM을 전혀 호출하지 않는 순수 알고리즘 인프라라 **종량 과금 충돌이 원천적으로 없다**(중립적 궁합). 다만 그만큼 LLM 리서치를 도와주지도 않는다. talon 구조상 Claude/Codex 기반 리서치·시황·매매근거 생성 레이어는 nautilus **바깥**에 두고, 그 산출물을 CustomData/Signal로 실행·백테스트 레이어에 주입하는 경계 분리가 자연스럽다. 즉 nautilus의 CustomData 파이프라인은 "LLM 판단 ↔ 결정론적 실행/검증"을 잇는 이상적인 인터페이스 청사진을 제공한다(코드 채택이 아니라 아키텍처 참고로서).

## 9. 결론

**verdict: reference / borrow**. talon은 nautilus를 프레임워크로 채택하지 말고, 자체 경량 이벤트 드리븐 백테스터/실행기를 만들되 위 "Borrow" 항목(시간축 규율, 바 체결 모델, Fill/Fee/Latency 3분할, 리스크 게이트, CustomData 시그널 주입, 리서치-라이브 패리티)을 설계 원칙으로 이식하는 것이 최적. 한국(토스) 어댑터·LLM 리서치 레이어·1인 스코프는 talon이 직접 채워야 하는 영역이며, 이 부분에서 nautilus는 참고 자산일 뿐 대체재가 아니다.
