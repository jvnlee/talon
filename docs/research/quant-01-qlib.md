# qlib 정밀 분석 (talon 설계 관점)

- 대상: microsoft/qlib (Quantitative-research Platform, MIT)
- 분석 시점 스냅샷: main @ d5379c5 (2026-04-22 push), stars ~46k, open issues ~461, `archived: false`
- 성격: Microsoft가 주도하는 **크로스섹셔널 ML 팩터 리서치/백테스트 프레임워크**. 중국 A주 일봉 유니버스 랭킹(topk) 전략이 설계의 중심.

---

## 1. 모듈 구조 (약 56k LOC)

- `qlib/data` — 데이터 계층. 표현식 엔진(`base.py`, `ops.py`), 바이너리 스토리지(`storage/file_storage.py`), 캐시(`cache.py`), Point-in-Time(`pit.py`), 데이터셋(`dataset/handler.py·loader.py·processor.py`).
- `qlib/backtest` — 체결 계층. `exchange.py`(체결·비용·거래제한), `executor.py`(중첩 이벤트 실행기), `account.py`/`position.py`, `decision.py`(주문), `signal.py`.
- `qlib/strategy` + `contrib/strategy` — `TopkDropoutStrategy`, `WeightStrategyBase`, `EnhancedIndexingStrategy`, 포트폴리오 옵티마이저.
- `qlib/model` + `contrib/model` — 모델 추상화 + 구현 ~30종(LightGBM/XGBoost/CatBoost, LSTM/GRU/ALSTM/Transformer/TCN/TabNet/TRA/HIST 등 PyTorch).
- `qlib/rl` — 주문 집행용 강화학습(order_execution).
- `qlib/workflow` — MLflow 기반 실험 관리(`recorder.py`, `record_temp.py`의 `SignalRecord`/`PortAnaRecord`).
- `qlib/contrib/online` — 온라인 **모델 롤링/재학습** 서빙. **실제 브로커 주문 어댑터는 아님**.

## 2. 데이터 파이프라인

- **스토리지**: 종목×필드마다 `<instrument>/<field>.<freq>.bin` 바이너리(리틀엔디언 float32, `np.fromfile`/`struct`). 캘린더 정수 인덱스 기반. 매우 빠르지만 CSV → `dump_bin.py`로 사전 덤프가 필요한 **일괄 배치형**. 라이브 증분 갱신에는 부적합.
- **컬렉터**: `scripts/data_collector`에 yahoo(US/CN), baostock, cn_index, us_index, crypto, fund, pit. **한국(KRX) 컬렉터·캘린더·휴장일은 없음.**
- **캐시**: 표현식/데이터셋 2단(전역 `H` 메모리 캐시 + 디스크 캐시).

## 3. 퀀트 코어 (핵심 관전 포인트)

### 3.1 표현식/알파 엔진
- `Expression`을 최상위로 하는 **지연 평가 AST**. `Feature`는 `$close`, `PFeature`는 `$$`(재무). Python 연산자 오버로딩으로 `$close/Ref($close,1)-1` 같은 문자열 수식을 AST로 파싱·평가.
- 연산자 풍부: `Rolling`(Mean/Std/Skew/Kurt/Max/Quantile/WMA/EMA), `Ref`, `Corr`, `Cov`, `Slope`, `Rsquare`, `Resi`, `TResample` 등. `register_all_ops`로 확장 가능.
- **알파 팩터 정의가 순수 선언형**: `Alpha158`/`Alpha360` 핸들러는 `["Corr($close, Log($volume+1), %d)"...]`처럼 수식 문자열 리스트로 팩터군을 생성. 팩터 = 코드가 아니라 데이터.

### 3.2 룩어헤드 방지 (설계의 백미)
- 모든 연산자가 `get_extended_window_size()`(왼쪽·오른쪽 확장폭)와 `get_longest_back_rolling()`을 구현. `Rolling(N)`은 `lft_etd += N-1`, `Ref(N)`은 `rght_etd -= N`로 **필요한 과거 구간을 정확히 산정해 미리 로드**하고 경계에서 미래를 참조하지 않도록 보장. EMA는 `(1-α)^w==1e-6`로 실효 윈도를 역산.
- **Point-in-Time**: `pit.py`의 `P` 연산자가 `<period_time, feature>`(예: 2020Q1)를 관측시점 `t`로 붕괴시켜 재무 데이터의 시점 정합을 지킴. `Ref($$x, -1)`처럼 **미래 분기 참조는 명시적으로 예외 발생**. 실적 발표 지연/정정에 의한 리비전 룩어헤드를 원천 차단.

### 3.3 데이터 핸들러(`DataHandlerLP`)와 누수 방지
- 3개의 데이터 키: **DK_R(raw) / DK_I(infer) / DK_L(learn)**. `shared_processors` + `infer_processors` + `learn_processors`를 조합(`append`/`independent`). 예: 정규화는 공유, `DropnaLabel`은 학습 전용.
- 프로세서는 `fit()`(train에서 통계 학습) / `__call__()`(apply) 분리. `ZScoreNorm`, `RobustZScoreNorm`, `CSZScoreNorm`/`CSRankNorm`(크로스섹셔널) 등. **train에서 fit → test에 apply** 구조라 정규화 통계의 미래 누수를 방지.

### 3.4 ML 모델 통합
- `Model.fit(dataset)` / `predict(dataset, segment)` 단일 계약. `dataset.prepare(["train","valid"], data_key=DK_L)`로 x/y/weight 취득. 모델 종류와 무관하게 동일 인터페이스 → 백테스트·기록과 느슨한 결합. GBDT부터 딥러닝까지 플러그인. **qlib 코어 자체는 LLM을 전혀 쓰지 않음**(LLM 팩터 마이닝은 별도 RD-Agent, 이는 API 과금 방식).

## 4. 백테스트 엔진

- **이벤트 기반(벡터화 아님)**. `NestedExecutor`로 다층(일봉 전략 → 분봉 집행) 중첩, 캘린더 스텝 루프 + 제너레이터(`_collect_data`). 벡터화 대비 느리지만 다주기·중첩 집행·실집행 근접에 유리.
- **체결 모델**(`exchange.py`): `deal_price`(vwap/close/open, `Ref`로 시점 지정) 선택. 현금 제약 하에서 `_calc_trade_info_by_order`가 `deal_amount`를 조정.
- **비용**: `open_cost`(기본 0.0015), `close_cost`(0.0025, 중국 인지세 포함), `min_cost`(5). **슬리피지=`impact_cost * (trade_val/total_trade_val)^2`** — 참여율의 2차식(마켓임팩트). `trade_unit`(중국 100주 단위) 라운딩.
- **거래 가능성 체크**: `check_stock_limit`(상·하한가 `limit_buy`/`limit_sell`), `check_stock_suspended`(NaN close=거래정지), `limit_threshold`(±X%). 상한가 매수 불가/정지 배제를 현실적으로 반영.
- **기록/재현성**: `PortAnaRecord`가 `backtest` + `risk_analysis`(연율 수익·IR·MDD 등)를 MLflow 아티팩트로 저장. `SignalRecord`는 IC/RankIC 산출.

## 5. 리스크 관리 · 라이브

- **리스크**: `model/riskmodel`의 공분산 추정(shrinkage, POET, structured) + `contrib/strategy/optimizer` 포트폴리오 최적화, `EnhancedIndexingStrategy`, `cost_control.py`. **포트폴리오/공분산 레벨**이지, 개인 트레이더식 손절·포지션사이징·트레일링스톱은 아님.
- **라이브**: 실제 증권사 주문 게이트웨이 없음. `contrib/online`은 모델 롤링 재학습, `rl/order_execution`은 집행 시뮬레이터. **실전 주문·체결·잔고 연동은 사용자가 직접 구축**해야 함.

## 6. 유지보수 상태

매우 활발(46k stars, MS 후원, 2026-04 최근 push, 미아카이브). 다만 의존성이 무거움(mlflow, redis, torch), Python ≥3.8, 중국 시장 디폴트가 곳곳에 박혀 있음(`trade_unit=100`, `close_cost`에 인지세).

---

## 7. talon 적용성 평가

talon은 **1인용·KR+US·단타/스윙·초기 재량매매 + 구독제 LLM 리서치**. qlib은 **유니버스 크로스섹셔널·일봉·ML 랭킹 포트폴리오**. 매매 스타일·시장·규모가 근본적으로 다르므로 **통째 채택(adopt)은 부적합**하고, **패턴 차용(borrow)** 이 정답이다.

### 그대로 채택 — 없음
프레임워크 전체는 과설계. redis/mlflow/torch 스택과 A주 유니버스 가정이 1인 감시종목 단타에 과함.

### 패턴만 빌릴 것 (핵심 자산)
1. **표현식 팩터 엔진**: 문자열 DSL → 지연평가 AST. talon의 지표/시그널을 코드가 아닌 선언형 데이터로 정의 → 백테스트/실시간 동일 코드로 재사용. 축소 이식 강력 추천.
2. **룩어헤드 안전 윈도 부기**: `get_extended_window_size`/`get_longest_back_rolling` 패턴은 백테스트→페이퍼→실전 전환의 정합성을 보장하는 talon 검증 로드맵의 핵심. 반드시 차용.
3. **PIT 재무 처리**: 실적 발표 지연·정정 룩어헤드 차단 설계. 스윙 매매의 펀더멘털 시그널에 유효.
4. **DataHandlerLP의 learn/infer 분리 + fit/apply 프로세서**: 정규화 통계 미래 누수 방지. talon의 모든 특징 전처리에 이 규율을 적용.
5. **비용/거래가능성 모델**: 2차식 마켓임팩트, `min_cost`, 로트 라운딩, 상·하한/정지 배제. **한국 ±30% 상하한, 매도세 ~0.18%, US 소수점 거래**로 파라미터만 갈아끼우면 그대로 유용.

### 참고만 할 것
- 중첩 이벤트 실행기(다주기 집행) 구조 — talon이 분봉 단타로 갈 때 참고.
- `TopkDropoutStrategy` — 시그널 → 주문 변환의 참조 구현.
- MLflow 기반 recorder/`PortAnaRecord` — 백테스트 재현성·성과지표(IR/MDD/IC) 산출 방식 벤치마크.

### 피할 것 (이유)
- **바이너리 `.bin` 스토리지 + 데이터 서버**: 전체 유니버스 일봉 배치에 최적화. 1인 감시종목엔 과함 → SQLite/Parquet가 단순·충분. Toss OpenAPI 실시간 증분과도 부정합.
- **30종 딥러닝 파이프라인**: GPU·데이터량 요구 과다. 초기 시드 1000만원·감시종목 소수엔 LightGBM 정도로 충분.
- **RD-Agent(LLM 팩터 마이닝)**: **API 종량 과금 방식** → 구독제 제약과 정면 충돌. 채택 불가. (다만 qlib 코어는 LLM-무관이므로 Claude Code/Agent SDK로 팩터 아이디어를 생성해 qlib식 DSL로 표현하는 하이브리드는 가능.)
- **크로스섹셔널 topk 포트폴리오 전략 엔진**: 단타/스윙 단일종목·재량 판단과 불일치.

### 한국 시장 적용성
KRX 컬렉터·캘린더·휴장일 부재, `trade_unit=100`(한국은 1주), 결제 T+2, ±30% 상하한(중국 ±10%와 다름), 세금 체계 상이. **Toss OpenAPI를 감싸는 KR 데이터/집행 어댑터를 신규 구축**해야 하며 qlib은 그 위 계층의 표현식·검증·비용 모델 패턴만 이식하는 것이 현실적이다.

### 구독제 LLM 궁합
qlib 퀀트 코어는 LLM 비의존 → **구독제 제약과 충돌 없음(양호)**. LLM은 qlib 밖에서 시황/뉴스/근거 제시를 맡고, qlib식 결정론적 팩터·백테스트가 수치 검증을 담당하는 **역할 분리**가 talon 설계에 이상적.
