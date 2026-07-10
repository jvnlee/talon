# FinRL 정밀 분석 (talon 설계 관점)

- 대상: [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL)
- 분석 시점 커밋: 2026-04-05 (활발히 유지보수 중, Columbia AI4Finance 재단 후원)
- 성격: **심층강화학습(DRL) 기반 자동매매 프레임워크**. Qlib류의 팩터/알파·백테스팅 프레임워크가 **아니다**.

## 1. 전체 아키텍처

FinRL은 3계층(Data → Environment → Agent) 구조다.

1. **Data layer** (`finrl/meta/data_processors/`, `preprocessor/`): 데이터 소스 어댑터. yahoofinance, alpaca, wrds, ccxt(암호화폐), eodhd, joinquant(중국), sinopac/shioaji(대만) 등. `DataProcessor` 파사드가 소스를 선택하고 `download_data → clean_data → add_technical_indicator → add_turbulence/vix → df_to_array`로 통일한다.
2. **Environment layer** (`finrl/meta/env_*/`): OpenAI Gym 환경. 시장을 시뮬레이션한다. 핵심은 `StockTradingEnv`.
3. **Agent layer** (`finrl/agents/`): DRL 알고리즘 래퍼. stable-baselines3(A2C/PPO/DDPG/TD3/SAC), ElegantRL, RLlib, 그리고 포트폴리오 최적화용 자체 정책경사(EIIE) 구현.

부수적으로 `paper_trading/`(Alpaca 라이브 어댑터), `plot.py`(pyfolio 성과 리포트), `applications/`(엔드투엔드 예제)가 있다.

## 2. 전략 표현 방식 — 룰이 아니라 "학습된 정책"

FinRL에는 전략을 명시적으로 기술하는 DSL이나 시그널 규칙이 없다. **전략 = 학습된 신경망 정책**이다.

`StockTradingEnv`(env_stocktrading.py) 기준:
- **State** = `[현금] + [종목별 종가] + [보유수량] + [기술지표들]` 을 1차원 벡터로 펼친 것.
- **Action** = 종목별 연속값 `[-1, 1]` → `hmax` 곱하고 정수화하여 매수/매도 주식 수.
- **Reward** = 스텝 간 총자산 변화(`end_total_asset - begin_total_asset`) × reward_scaling.

즉 "무엇을 사고팔지"를 RL 에이전트가 종단간(end-to-end)으로 학습한다. 사람이 읽을 수 있는 매매 근거는 **산출되지 않는다**(블랙박스 MLP 정책).

포트폴리오 최적화 환경(`env_portfolio_optimization.py`)은 더 정교하다. 관측이 `(features, n_stocks, time_window)` 3차원 텐서, action이 포트폴리오 비중 벡터(n+1, 현금 포함), EIIE(CNN) 아키텍처 + 정책경사. 수수료 모델도 `trf`(transaction remainder factor)/`wvm` 두 가지를 갖춘다.

## 3. 백테스팅 엔진 — 별도 엔진이 없다

이 부분이 talon 관점에서 가장 중요하다. **FinRL에는 독립적인 백테스팅 엔진이 없다.** "백테스트"란 학습된 에이전트를 Gym 환경에서 하루씩 `step()` 전진시키는 단일 패스 시뮬레이션(`DRLAgent.DRL_prediction`)이고, 그 결과 자산곡선을 pyfolio/quantstats로 통계 처리하는 것이 전부다.

체결/비용 모델을 코드에서 직접 확인한 결과:

- **체결 모델**: 매우 순진하다. 주문은 **당일 종가**(`self.state[index+1]`)에 **전량·즉시** 체결된다. 부분체결·미체결·호가·시장충격 모델이 전무하다(`grep slippage|bid.ask|partial fill|market impact` → 0건).
- **수수료**: 단순 정률(`buy_cost_pct`, `sell_cost_pct`), 종목별 배열 지원. 예제 기본값 ~0.1%.
- **슬리피지**: 표준 주식 환경에 **없음**. 포트폴리오 환경만 수수료 모델 2종 보유, 슬리피지는 여전히 없음.
- **주문 순서**: 매도를 먼저(argsort로 action이 음수인 것부터) 처리해 현금을 확보한 뒤 매수. 이 순서 로직은 합리적이다.
- **리스크 컷**: turbulence 지수가 임계치를 넘으면 전 종목 청산. 사실상 유일한 리스크 관리.

**룩어헤드(look-ahead) 위험**:
1. 같은 바(bar) 내에서 t일 종가로 상태를 만들고 t일 종가에 체결한다 — 의사결정과 체결이 동일 바 종가에 걸쳐 낙관적이다(데이트레이딩 검증에는 특히 부적절).
2. `FeatureEngineer`가 train/test 분할 **이전에** 전체 기간에 대해 지표를 계산한다. 기술지표 자체는 stockstats의 롤링(인과적) 계산이라 대체로 안전하나, `GroupByScaler`/`MaxAbsScaler`를 전체 데이터에 fit하면 **정규화 누수**가 생긴다.
3. turbulence는 과거 252일 공분산 기반이라 인과적이다. VIX는 date로 merge(당일 값)이라 안전.

결론: FinRL의 백테스트 수치는 **검증 등급이 아니라 낙관 편향**이 크다. 단타 전략의 현실적 검증(체결 지연·슬리피지·유동성)에는 그대로 쓸 수 없다.

## 4. 퀀트 코어 — 팩터 엔진이 아니라 "특징 → RL"

talon이 원하는 팩터/알파·표현식 엔진·포인트인타임 DB 관점에서 보면 FinRL은 빈약하다.

- **알파/팩터 정의**: 표현식 DSL 없음. "특징"은 곧 기술지표(macd, boll_ub/lb, rsi_30, cci_30, dx_30, sma)로 stockstats가 계산한 값 + turbulence + VIX. 선택적으로 재무비율(`fundamental_stock_trading.py`가 WRDS Compustat 분기 데이터로 OPM/NPM/ROA/ROE 계산).
- **데이터 핸들러**: `DataProcessor` 파사드 + 소스별 프로세서. 인터페이스가 깔끔하고 플러그블하다(이 점은 빌릴 가치가 있다).
- **ML 모델 통합**: 별도의 지도학습 알파 모델이 없다. RL 정책망 자체가 "모델"이며 특징→행동을 종단간 학습한다. Qlib처럼 "알파 예측 → 포트폴리오 최적화" 파이프라인 분리가 없다.
- **포인트인타임 처리**: 취약하다. PIT 재무 DB 없음. 펀더멘털 예제는 정적 CSV를 로드할 뿐 정정공시(restatement)·공시시점 처리를 하지 않는다.

## 5. 라이브/페이퍼 트레이딩 어댑터

`paper_trading/alpaca.py`가 실사례다. 학습된 모델 로드 → Alpaca에서 최신 1분 데이터 fetch → 상태 구성 → `model.predict` → 스레드로 시장가 주문 제출. 장 마감 2분 전 전량 청산, turbulence 넘으면 청산 로직 포함. Alpaca(미국) 전용이며 스레드 기반 주문은 견고성이 낮다. **토스 OpenAPI 어댑터는 처음부터 새로 써야 한다.**

## 6. 유지보수 상태 & 코드 품질

활발히 유지보수(최근 커밋 2026-04)되고 커뮤니티가 크다. 그러나 **연구용 코드 품질**이다: 주석 처리된 죽은 코드 다수, `gym`(포트폴리오 env)과 `gymnasium`(주식 env) 혼용, `data_processor.py`에 `add_turbulence`/`add_vix`가 중복 정의, `save_state_memory`에 Bitcoin/Gold 컬럼명 하드코딩. 프로덕션 하드닝은 되어 있지 않다.

## 7. talon 적용성 평가

### 한국 시장 적용성
- 네이티브 한국 데이터 소스가 없다. yfinance로 KRX 티커(예: `005930.KS`) 일봉은 가능하나, 토스 프로세서를 직접 구현해야 한다. 다행히 `DataProcessor` 인터페이스가 명확해 어댑터 작성은 쉽다(빌릴 패턴).
- 가격제한폭(±30%), 호가단위, T+2, 공매도 제한 등 한국 시장 미세구조가 **전혀 모델링되어 있지 않다**. 단타 검증엔 치명적.

### 구독제 LLM 제약과의 궁합
- FinRL은 **LLM을 전혀 쓰지 않는다.** 따라서 API 종량 과금 이슈와 무관(중립)하다. 반대로 talon의 핵심인 "LLM이 리서치·지표·차트를 추론하고 근거를 제시" 설계와도 **접점이 없다**. 패러다임이 다르다: FinRL은 설명 불가능한 신경 정책을 학습, talon은 설명 가능한 근거 + 사람 최종판단.
- RL은 샘플 비효율·백테스트 과적합·해석 불가라서 talon의 "매매 근거 제시" 요구와 정면 충돌한다.

### 최종 판정: **참고/부분 차용(reference/borrow)** — 그대로 채택(adopt) 아님

**빌릴 패턴(borrow)**
1. `DataProcessor` 어댑터 인터페이스(`download→clean→indicator→array`) — 토스 프로세서 설계 템플릿으로 유용.
2. **Turbulence 지수**(수익률의 마할라노비스 거리, 252일 롤링, 인과적) — 시장 레짐 리스크 신호로 저렴하고 유용. talon에서 LLM 입력 피처 및 리스크 컷 트리거로 재사용 가능.
3. `FeatureEngineer`의 stockstats 기반 기술지표 계산을 피처 라이브러리로.
4. pyfolio/quantstats 티어시트 성과 리포트.
5. 매도 선처리→매수 체결 순서 로직.

**참고만(reference)**
- 앙상블 전략(롤링 윈도우로 검증 Sharpe 최고 모델 선택)의 메타 패턴, turbulence 청산 리스크 컷.

**피할 것(avoid)**
1. Gym env를 백테스트 엔진으로 쓰기 — 동일 바 종가 전량체결·무슬리피지·단일패스라 단타 검증엔 낙관 편향 과다.
2. RL 에이전트를 의사결정 코어로 — 과적합·설명 불가.
3. gym/gymnasium 혼용 등 연구용 코드 위생.
4. FinRL 백테스트 수치를 검증 등급으로 신뢰하기.

**talon 시사점**: talon은 별도의 이벤트 기반 백테스터(현실적 체결·슬리피지·호가단위·가격제한폭)와 명시적 시그널/팩터 표현을 자체 설계해야 한다. FinRL에서 가져올 것은 데이터 어댑터 패턴, turbulence 신호, 성과 리포팅 정도로 국한된다.
