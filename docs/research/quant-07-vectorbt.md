# vectorbt 정밀 분석 (talon 설계 관점)

- 레포: https://github.com/polakowo/vectorbt (polakowo, Oleg Polakow)
- 분석 버전: **1.1.0** (2026-07-05 커밋), 저작권 2017–2026
- 라이선스: **Apache 2.0 + Commons Clause** (fair-code)
- 정체성: 상용 **VectorBT PRO**의 오픈소스 커뮤니티 에디션. "Thinks in matrices, backtests at scale."
- 스택: Python 3.11–3.14, numpy≥2.4, pandas≥3.0, Numba, 선택적 **Rust 엔진**(`vectorbt-rust`), Plotly

---

## 1. 아키텍처 개요

pandas 네이티브 + 컬럼-벡터화가 핵심 철학이다. 모듈 구조:

- `base/` — 브로드캐스팅, `ArrayWrapper`, `ColumnGrouper`. **모든 파라미터 조합을 DataFrame의 "컬럼"으로 표현**하는 추상화의 뿌리.
- `data/` — `Data` 베이스 + 어댑터(`YFData`, `BinanceData`, `CCXTData`, `AlpacaData`, `GBMData`/`SyntheticData`), `DataUpdater`(스케줄 갱신).
- `indicators/` — `IndicatorFactory`. `from_apply_func`/`from_custom_func`/`from_talib`/`from_pandas_ta`/`from_ta`. `run_pipeline`이 파라미터 조합·브로드캐스팅·결과 concat 담당.
- `signals/` — 시그널 생성·랭킹·매핑·분포 분석.
- `labels/` — **ML 지도학습용 라벨 생성**(미래 수익률, 추세, 브레이크아웃).
- `portfolio/` — 백테스트 엔진. `base.py`(247KB, `from_signals`/`from_orders`/`from_order_func`/`from_holding`/`from_random_signals`), `nb.py`(290KB, Numba 코어), `enums.py`(주문/수수료/슬리피지/스톱 모델), `trades.py`/`orders.py`/`logs.py`(이벤트 레코드).
- `returns/` — Sharpe/Sortino/Calmar/Omega/**Deflated Sharpe** + QuantStats 어댑터.
- `records/` — 컬럼형 이벤트 저장(주문·체결·드로다운·로그).
- `messaging/telegram.py` — `TelegramBot`(python-telegram-bot 기반).
- `utils/schedule_.py` — asyncio 기반 스케줄러(라이브 갱신 루프).

## 2. 백테스팅 엔진 설계 (핵심)

**"설정 축은 벡터화, 시간 축은 순차"** 가 정확한 요약이다.

- 이벤트기반 vs 벡터화: 흔한 오해와 달리 순수 벡터연산이 아니다. `simulate_from_signals_nb`는 그룹별로 `i=0..N` **바를 순차 루프**한다. 대신 수천 개의 자산×파라미터 조합을 2D NumPy 배열의 컬럼에 담아 Numba(또는 Rust)로 **조합을 동시에** 처리한다. 그리드서치가 초 단위로 끝나는 이유.
- **룩어헤드 방지**: 시간 순차 루프이므로 바 `i`의 상태는 `≤i` 데이터에만 의존 → 구조적으로 인과적. `Order.price`는 `-inf`→당일 시가, `+inf`→당일 종가로 치환되며, docstring이 "시가·종가 사이 타임스탬프를 쓰라"고 명시적으로 룩어헤드를 경고한다.
- **체결 모델**(`execute_order_nb`/`buy_nb`): `Order` NamedTuple에 size, price, `size_type`(Amount/Value/Percent/Target{Amount,Value,Percent} 6종), direction, fees, fixed_fees, slippage, min_size, max_size, `size_granularity`, `reject_prob`, lock_cash, allow_partial 포함.
- **수수료·슬리피지**: 슬리피지는 가격 페널티 — 매수 `price*(1+slippage)`, 매도 `price*(1-slippage)`. 수수료는 `거래대금*fees + fixed_fees`(정률+정액, 음수 허용). 현금 부족 시 부분체결 자동 계산.
- **리스크 관리 내장**: `sl_stop`, `sl_trail`(트레일링), `tp_stop`, `stop_entry_price`, `stop_exit_price`, `upon_stop_exit/update` + 동적 조정 콜백 `adjust_sl_func_nb`/`adjust_tp_func_nb`. 스톱은 인트라바 high/low로 평가. 벡터화 엔진에 진짜 스톱로직이 들어있는 건 드문 강점.
- **포트폴리오/현금공유**: `group_lens`, `call_seq`(주문 실행 순서, `auto_call_seq`로 가치순 정렬)로 멀티에셋 자본배분 지원.
- `size_granularity`(로트 반올림), `reject_prob`(확률적 주문 거부)는 현실적 체결 모델링 요소.

## 3. 퀀트 코어

- **팩터/알파 표현**: OSS에는 **표현식 문자열 엔진이 없다**(`from_expr`, WQA101 알파는 PRO 전용, grep 확인). OSS는 팩터를 `IndicatorFactory`로 만든 지표 클래스 → 불리언 시그널 배열 → 포트폴리오로 표현한다. 파라미터 스윕은 조합을 컬럼에 넣는 방식(`MA.run_combs(window=windows, r=2)` 등).
- **데이터 핸들러**: `Data` 서브클래스에 `download_symbol`(classmethod)/`update_symbol`(instance)만 구현하면 `download`/`update`/`get`/`concat`/플롯/스케줄갱신을 전부 상속. 멀티심볼 정렬은 `get()`/`concat()`, 캘린더 정렬은 `missing_index="drop"`.
- **포인트인타임**: **진짜 bitemporal/as-of 처리는 없다.** 생존편향·정정(restatement)·상장폐지 복원은 사용자 책임. 라이브 갱신은 append 방식(`DataUpdater`).
- **ML 통합**: `labels/`가 미래참조 타깃(future_mean/std/min/max, 추세·브레이크아웃 라벨)을 생성 — 이건 시그널이 아니라 **학습 라벨**이라 일부러 룩어헤드다. scikit-learn이 의존성이지만 피처스토어·CV 파이프라인은 없다(BYO 모델). **Deflated Sharpe Ratio**(Bailey & López de Prado)가 내장돼 대규모 스윕의 과최적화를 통계적으로 방어.
- **라이브 실행 어댑터**: **없다.** 브로커 주문 라우팅 코드가 레포 전체에 부재(`submit/place/create_order` grep 결과 0). 데이터 입력 → 백테스트/분석 → 알림 출력까지만. 실주문은 사용자/외부 코드 몫.

## 4. 유지보수 상태

프로덕션/안정(Development Status 5). 2026-07 최신 커밋, numpy 2.4·pandas 3.0·Python 3.14까지 대응하는 매우 현대적 스택. 다만 커뮤니티 에디션이라 개발 리소스가 PRO에 집중돼 신기능(표현식 엔진 등)은 PRO 전용으로 빠져 있음.

## 5. talon 적용성 평가

### 그대로 채택 (백테스팅 엔진 역할)
talon의 "광범위한 백테스팅 검증" 단계에 **vectorbt를 백테스트·분석 라이브러리 의존성으로 채택**하는 것이 최선. 성숙·고속·인과적이며 KR+US OHLCV·전 지표·스톱·전 성과지표를 커버한다. 이걸 재구현하는 건 낭비다.

### 패턴만 빌릴 것
- **Data 어댑터 패턴** → **TossData** 어댑터 작성(`download_symbol`/`update_symbol`). 토스 OpenAPI가 vectorbt 생태계 전체에 무료로 연결된다.
- **DataUpdater + ScheduleManager**(asyncio) → 단타용 장중 폴링·스케줄 데이터 갱신 루프.
- **Order/enums 모델**(size_type·fees·slippage·size_granularity·reject_prob·스톱) → talon 자체 페이퍼/실전 체결모델 스펙의 참고 골격.
- **labels 모듈** → 지도학습 알파를 할 경우 타깃 생성.
- **Deflated Sharpe + walk-forward** → 다전략 스윕의 과최적화 방어(talon 로드맵상 필수).
- **TelegramBot** → 인터페이스 패턴 참고(단, talon은 LLM 에이전트 연동이 필요해 자체 구현 가능성 큼).

### 참고만 할 것
Numba/Rust 컬럼-벡터화 엔진 내부는 talon이 재현하지 말고 호출만.

### 피할 것 / 주의
- **라이브 실행에 쓰지 말 것** — 주문 라우팅이 없다. talon은 Toss 주문 API 기반 실행 계층(페이퍼→실전)을 직접 만들어야 한다.
- **한국시장 갭**: (1) KRX 캘린더·휴장일·T+2 결제·**상하한가 ±30%**·**호가단위(틱)** 미지원 → talon이 공급해야 함. (2) 수수료가 **대칭**이라 매도 편측 **거래세(0.18~0.23%)** 를 자연스럽게 못 넣음 → 바별/편측 `fees` 배열로 우회하거나 사후보정 필요. (3) KRW 무소수주 → `size_granularity=1`로 로트 처리 가능.
- **의존성 무게**: numpy 2.4/pandas 3.0/numba/plotly 핀이 최신·공격적 → 격리 환경(별도 venv) 권장.
- **라이선스**: 개인 1인용 talon엔 문제없음. 단 "이 소프트웨어가 가치의 대부분인 제품/서비스 판매" 금지 → talon의 가치는 LLM 에이전트·Toss 통합·KR 로직에 두어야 상업화 여지 유지.

### 구독제 LLM 제약과의 궁합
**완벽하게 상보적.** vectorbt는 순수 파이썬 수치연산으로 **LLM 토큰·API 비용이 전혀 없다.** 종량 API 금지 제약과 무충돌. 역할 분담이 명확하다 — LLM(Claude Code/Agent SDK)은 리서치·추론·근거 제시, vectorbt는 결정론적 계산(백테스트·지표·성과). README가 명시적으로 "AI 에이전트 주도 워크플로용 조합 가능 API"를 표방해 에이전트가 도구로 호출하기 쉽다. talon 아키텍처(에이전트가 판단, 사용자가 최종 주문)와 정합적.

## 결론

**verdict: adopt** — 백테스팅·리서치 엔진으로 라이브러리를 그대로 채택하고, 데이터 어댑터·스케줄러·주문모델·과최적화 방어 패턴을 빌린다. 단 **라이브 실행 용도로는 쓰지 말고**, 한국시장 캘린더·틱·거래세·주문 라우팅은 talon이 직접 채워야 한다.
