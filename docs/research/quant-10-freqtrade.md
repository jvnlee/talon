# freqtrade 정밀 분석 (talon 설계 관점)

- 대상 레포: https://github.com/freqtrade/freqtrade (분석 커밋 `70d86c3`, 2026-07-09)
- 규모: `freqtrade/` 하위 331개 파일, 약 65k LOC. Python 3.11+, pandas 2/3, ccxt 4.5, scikit-learn/LightGBM/XGBoost/PyTorch.
- 라이선스: **GPLv3 (copyleft)**. 유지보수: 매우 활발(분석일 당일에도 PR 머지), 단 최근 커밋 저자는 사실상 단일 메인테이너(xmatthias)에 집중 — 버스팩터 리스크.
- 성격: **암호화폐 전용** 룰/ML 기반 트레이딩 봇. LLM은 전혀 사용하지 않음.

## 1. 모듈 구조와 플러그인 아키텍처

핵심은 `resolvers/`의 동적 로딩 패턴이다. Strategy, Exchange, PairList, Protection, FreqaiModel이 모두 `IResolver`를 통해 이름 문자열로 런타임 주입된다. 사용자는 규정된 인터페이스만 구현하면 코어 수정 없이 확장한다. 이 "인터페이스 + 리졸버" 구조는 talon이 그대로 빌릴 가치가 있다(전략/브로커/시그널을 플러그인화).

주요 모듈: `strategy/`(전략 인터페이스), `optimize/`(백테스트·하이퍼옵트·바이어스 분석), `freqai/`(ML 코어), `data/`(데이터 파이프라인), `exchange/`(거래소 어댑터), `plugins/`(페어리스트·프로텍션), `persistence/`(SQLAlchemy Trade 모델), `rpc/`(텔레그램·REST API), `freqtradebot.py`(라이브 루프).

## 2. 전략 표현 방식

**표현식/수식 DSL이 아니라 Python 클래스 상속**이다(`strategy/interface.py`, `IStrategy`). 전략은 pandas DataFrame에 컬럼을 채우는 방식:

- `populate_indicators(df, metadata)` → 지표 컬럼 생성(벡터화, TA-Lib/qtpylib).
- `populate_entry_trend` / `populate_exit_trend` → 불리언 시그널 컬럼(`enter_long`, `exit_long`, `enter_short` 등) 세팅.
- 풍부한 콜백 훅: `custom_stoploss`, `custom_exit`, `custom_stake_amount`, `adjust_trade_position`(피라미딩/DCA), `confirm_trade_entry/exit`, `leverage`, `adjust_entry_price`. 시그널(벡터)과 주문 판단(이벤트 콜백)을 분리한 설계가 핵심.
- 멀티 타임프레임: `@informative` 데코레이터와 `merge_informative_pair`로 상위 TF 지표를 정렬 병합.
- 하이퍼파라미터: `IntParameter/DecimalParameter/CategoricalParameter`로 전략 속성에 탐색공간을 선언 → 하이퍼옵트가 자동 최적화.

talon의 단타/스윙 룰을 이 "지표 채우기 + 시그널 컬럼 + 콜백" 패턴으로 표현하면 자연스럽고, LLM이 생성/수정하기도 쉬운 구조다.

## 3. 백테스팅 엔진 설계

`optimize/backtesting.py`(약 2k LOC). **하이브리드**다: 지표·시그널 계산은 벡터화(pandas), 체결 시뮬레이션은 이벤트 기반 캔들 루프(`backtest_loop`).

- **룩어헤드 방지(핵심)**: `_get_ohlcv_as_lists`에서 시그널 컬럼을 전부 `shift(1)` 한다. 즉 캔들 N에서 계산된 시그널은 캔들 N+1의 시가에 체결된다. 미래 정보 누출 원천 차단. startup 캔들 트림으로 지표 워밍업 구간도 제거.
- **체결 모델**: 진입/시그널 청산은 다음 캔들 **시가** 체결. 스탑로스/ROI는 캔들 OHLC로 캔들 내 최악 경로를 가정해 체결가 산출(`_get_close_rate_for_stoploss/_roi`) — 낙관적 편향을 억제. `timeframe_detail`로 하위 TF 캔들을 주입해 캔들 내 체결 정밀도를 높이는 옵션 제공.
- **수수료**: 주문당 단일 `fee` 비율 적용. **명시적 슬리피지 모델은 백테스트에 없음**(결정론적 체결가). 라이브에서는 호가/`price_side`로 처리. → talon에서 한국 시장 슬리피지·체결확률을 반영하려면 별도 확장 필요.
- **바이어스 자동 탐지(탁월)**: `optimize/analysis/lookahead.py`, `recursive.py`. 백테스트를 진입/청산 캔들까지 잘라 재실행하고, 전체 구간 실행 대비 지표값이 달라지는지 비교해 **룩어헤드/재귀(startup 부족) 바이어스를 프로그램적으로 검출**한다. talon이 반드시 채택할 아이디어.
- **하이퍼옵트 손실함수**: Sharpe/Sortino/Calmar/MaxDrawdown/멀티메트릭 등 13종(`optimize/hyperopt_loss/`). 목적함수 플러그인화가 잘 되어 있음.

## 4. 리스크 관리

- 스탑로스: 정적/트레일링/커스텀/거래소측(stoploss_on_exchange).
- `minimal_roi`: 보유시간 기반 목표수익 테이블(단타에 유용).
- **Protections**(`plugins/protections/`): `StoplossGuard`(연속 손절 시 락), `MaxDrawdownProtection`, `CooldownPeriod`, `LowProfitPairs`. 전역/페어별 `PairLocks`로 일정 시간 신규 진입을 봉쇄 — 연쇄 손실 차단 로직. talon의 일일 손실 한도·과매매 방지에 그대로 이식 가능.
- 포지션 사이징: `wallets.py`가 stake 계산·검증(min/max stake, 가용잔고, max_open_trades).

## 5. 퀀트 코어 (FreqAI)

`freqai/`는 **지도학습 ML 통합 레이어**다. alphalens/qlib류의 팩터-수식 엔진이 아니라, **수작업 피처 엔지니어링 + 워크포워드 재학습** 방식이다.

- **피처 정의**: 전략 훅 `feature_engineering_expand_all/basic/standard`, `set_freqai_targets`. 피처는 접두사 `%`, 타깃은 `&` 규약. 설정의 `indicator_periods_candles × include_timeframes × include_shifted_candles × include_corr_pairlist` 조합으로 피처를 **자동 팽창**시켜 하나의 정의가 수백 피처로 확장된다.
- **포인트인타임/워크포워드(핵심)**: `start_backtesting`이 `train_period_days`(학습창)와 `backtest_period_days`(예측창)를 슬라이딩하며 각 구간마다 **재학습→예측**을 반복하고 결과를 이어붙인다. 미래 데이터로 학습하는 룩어헤드를 구조적으로 차단하는 정석 설계. 모델은 `data_drawer`가 디스크 캐시/재사용.
- **데이터 정제 파이프라인**(`define_data_pipeline`, datasieve 사용, sklearn 유사): VarianceThreshold → MinMaxScaler → (옵션) PCA → SVM 이상치제거 → **Dissimilarity Index(DI, 분포 밖 예측 필터링)** → DBSCAN → 노이즈 주입. 예측 신뢰도를 out-of-distribution 관점에서 거르는 DI가 실전 배포에 유용.
- **모델**: LightGBM/XGBoost/CatBoost, PyTorch MLP/Transformer, 강화학습(stable-baselines3). 멀티타깃 회귀/분류 래퍼 제공.

## 6. 데이터 파이프라인 & 라이브 어댑터

- `data/dataprovider.py`: 히스토릭+라이브 OHLCV/trades/orderbook/ticker를 단일 추상화로 제공. 데이터 핸들러는 feather(기본)/parquet/json 교체형(`data/history/datahandlers/`).
- **라이브 루프**: `freqtradebot.process()`가 마켓 리로드→화이트리스트 갱신→캔들 refresh→`analyze`→주문관리→청산→진입을 스로틀링 반복.
- **거래소 어댑터**: `exchange/exchange.py`(4.1k LOC)가 **CCXT에 강하게 결합**(모듈 최상단 `import ccxt`, `self._api: ccxt.Exchange`, 모든 주문/시세가 ccxt 경유). 거래소별 차이는 `binance.py` 등 서브클래스가 오버라이드.

## 7. talon 적용성 평가

**시장 적합성**: freqtrade는 24/7·무장 마감·무배당·무권리락의 크립토를 가정한다. 한국/미국 주식은 정규장 시간·T+2·가격제한폭(KRX ±30% 상하한가)·공매도 규제·기업행위(액면분할/배당/유상증자)·틱사이즈 체계가 다르다. 캔들이 연속이라는 백테스트 전제가 갭·거래정지에서 깨지므로 캘린더/거래정지/수정주가 처리를 추가해야 한다. 다만 `shift(1)` 룩어헤드 방지와 이벤트 루프 체결 모델은 시장 무관하게 그대로 유효하다.

**브로커 결합**: 토스증권 OpenAPI는 CCXT 거래소가 아니다. `Exchange` 계층이 ccxt에 깊게 묶여 있어 라이브 어댑터를 재사용하려면 ccxt 호환 셔틀을 새로 쓰거나 `IExchange`류 신규 어댑터를 구현해야 한다 — 상당한 작업량. **코드 채택보다 인터페이스 패턴 차용이 현실적.**

**구독제 LLM 제약과의 궁합**: freqtrade는 LLM을 전혀 쓰지 않으므로 종량 과금 제약과 **직접 충돌이 없다**. 오히려 상보적이다 — freqtrade식 결정론적 엔진(시그널·백테스트·리스크)을 하부에 두고, talon의 LLM(Claude Code 구독)은 그 위에서 리서치·뉴스·매매근거 서술·최종 판단 보조를 담당하는 2계층이 이상적. FreqAI의 ML은 LLM과 직교하므로 팩터 예측용으로 병행 가능.

**라이선스 주의**: GPLv3 copyleft. 1인 비배포 개인 사용은 문제 없으나, 코드 스니펫을 talon에 직접 이식하면 talon 전체가 GPL 의무에 노출될 수 있다. **설계 아이디어 차용은 안전, 소스 복붙은 지양.**

### 결론: **borrow (패턴 차용)**

| 항목 | 판정 | 근거 |
|---|---|---|
| IStrategy 콜백 설계(시그널/주문 분리) | 차용 | 룰 표현·LLM 생성에 최적 |
| `shift(1)` 룩어헤드 방지 | 채택 | 시장 무관 정석 |
| lookahead/recursive 바이어스 자동탐지 | 채택 | talon 필수 검증장치 |
| 워크포워드 재학습(FreqAI) | 채택(아이디어) | 포인트인타임 정석 |
| Protections/PairLocks 리스크 락 | 차용 | 일손실한도·과매매방지 |
| 하이퍼옵트 손실함수 플러그인 | 차용 | 목적함수 교체형 |
| 리졸버 플러그인 아키텍처 | 차용 | 브로커/전략 확장성 |
| CCXT 거래소 계층(라이브) | 회피 | 토스 비호환, 크립토 전제 |
| 백테스트 코드 직접 이식 | 회피 | GPL + 연속캔들 전제 |

**요약**: freqtrade는 talon의 "퀀트 엔진 레이어"를 어떻게 짜야 하는지에 대한 최상급 참고서다. 룩어헤드 방지, 워크포워드, 바이어스 자동검증, 리스크 락, 플러그인 구조는 설계 원리로 적극 채택하되, 크립토·ccxt 종속 코드는 한국 주식·토스 API·정규장 캘린더에 맞춰 talon이 독자 구현해야 한다. LLM 계층은 이 엔진 위에 얹는 상보 관계로 두는 것이 구독제 제약과도 잘 맞는다.
