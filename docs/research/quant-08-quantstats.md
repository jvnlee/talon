# QuantStats 정밀 분석 (talon 설계 관점)

- 레포: https://github.com/ranaroussi/quantstats (Apache-2.0, 작성자 Ran Aroussi — yfinance 제작자와 동일)
- 분석 시점 버전: v0.0.81 / 최신 커밋 2026-01-13 / 약 8,000 LOC / 순수 Python
- 클론 위치: `/private/tmp/.../scratchpad/oss/quant-quantstats`

## 0. 한 줄 정의 (오해 방지)

**QuantStats는 백테스팅 엔진도, 전략/팩터 프레임워크도, 라이브 트레이딩 시스템도 아니다.** 이미 계산된 **수익률(returns) 시계열**을 입력받아 50여 개의 성과·리스크 지표를 계산하고 HTML 티어시트(tearsheet)를 뽑아주는 **사후 성과분석(portfolio analytics) 라이브러리**다. 임무 체크리스트의 상당수 항목(이벤트 기반 엔진, 체결/슬리피지/수수료 모델, 룩어헤드 방지, 팩터/알파 표현식 엔진, ML 통합, 포인트인타임 데이터, 라이브 어댑터)은 **이 레포에 존재하지 않는다.** 따라서 talon의 "퀀트 코어"로 볼 것이 아니라 **리포팅/평가 레이어**로 봐야 한다.

## 1. 모듈 구조

3개의 공개 모듈 + 내부 유틸로 구성된다.

- `stats.py` (3,300줄) — 지표 계산 엔진. Sharpe/Sortino/Calmar/Omega/VaR/CVaR/Kelly/최대낙폭/greeks(알파·베타) 등.
- `plots.py` + `_plotting/` — matplotlib/seaborn 시각화(낙폭, 롤링 지표, 월별 히트맵 등).
- `reports.py` (2,500줄) — `metrics()`(지표 표), `basic()`/`full()`, `html()`(완성형 티어시트) 생성. `report.html` 템플릿에 값 주입.
- `utils.py` — 데이터 정규화(`_prepare_returns`), yfinance 다운로드, `make_index`/`make_portfolio`.
- `_montecarlo.py` — 부트스트랩 몬테카를로.
- `_compat.py` / `_numpy_compat.py` — pandas 2.2+/numpy 2.0 버전 차이 흡수 셔틀.

특징적 설계: `extend_pandas()`가 `PandasObject`를 몽키패치해 `returns.sharpe()`, `returns.max_drawdown()`처럼 pandas 객체에서 직접 지표 메서드를 부를 수 있게 한다(`__init__.py:33`). 편의성은 좋으나 전역 부작용이므로 규모 있는 코드베이스에서는 `qs.stats.sharpe(returns)` 명시 호출을 권장.

## 2. 데이터 파이프라인

핵심은 `utils._prepare_returns()`(`utils.py:583`) 단일 정규화 함수다.

1. `copy()` 후 **가격/수익률 자동 판별**: `min>=0 and max>1`이면 가격으로 간주해 `pct_change()`(`:618`). 휴리스틱이라 취약 — 예를 들어 0~1 구간 수익률 인덱스에는 오작동 가능.
2. `inf → NaN → fillna(0)` (`:625-629`). 결측을 0 수익률로 채우는 이 방식은 거래정지·상장폐지 구간을 왜곡할 수 있어 주의.
3. `rf>0`이면 무위험이자 차감(`to_excess_returns`).
4. 타임존을 UTC-naive로 정규화.
5. `hash_pandas_object` 기반 스레드세이프 캐시(FIFO, 최대 100개)로 반복 호출 최적화(`:104-157`).

데이터 소스는 **yfinance 단 하나**다. `download_returns()`(`utils.py:664`)와 `_prepare_benchmark()`가 `_compat.safe_yfinance_download`(재시도 래퍼)로 종가를 받아 `auto_adjust=True` 수익률로 변환한다. **KRX/토스 OpenAPI 연동은 전혀 없다.** 즉 talon은 이 다운로드 레이어를 우회하고 자체 수익률 Series를 직접 넣어야 한다.

## 3. 지표(퀀트 코어) 상세

`stats.py`는 라이브러리의 진짜 자산이다. 정확한 벡터화 pandas/numpy 구현으로 다음을 제공한다.

- **위험조정수익**: `sharpe`(연율화 √252), `sortino`, `adjusted_sortino`, `smart_*`(자기상관 페널티 `autocorr_penalty`), `probabilistic_sharpe/sortino`(Bailey & López de Prado PSR), `omega`, `calmar`, `treynor`, `information_ratio`.
- **리스크**: `value_at_risk`(모수적 정규분포 가정, `norm.ppf(1-c, mu, sigma)` — `stats.py:1899`), `conditional_value_at_risk`/`expected_shortfall`, `risk_of_ruin`, `ulcer_index`/`upi`, `serenity_index`, `tail_ratio`.
- **낙폭**: `to_drawdown_series`, `max_drawdown`(팬텀 baseline으로 첫날 손실 엣지케이스 처리 `:2485`), `drawdown_details`(구간별 깊이·기간·회복일).
- **벤치마크 대비**: `greeks`(알파·베타를 `np.cov`로), `rolling_greeks`, `r_squared`, `compare`.
- **포지션 사이징**: `kelly_criterion`(`payoff_ratio`·`win_rate` 기반, `:2553`).
- **거래형 통계**: `win_rate`, `payoff_ratio`, `profit_factor`, `consecutive_wins/losses`, `best/worst`.

**중대한 캐비엇(#493, PROJECT.md에 명시)**: 이 "거래형" 통계는 실제 체결 단위가 아니라 **기간(일별 수익률) 단위**로 계산된다. 즉 승률·손익비가 "이긴 날 / 진 날"이지 "이긴 트레이드 / 진 트레이드"가 아니다. 단타 평가에는 부적합하며 talon이 트레이드 단위 통계를 별도로 구현해야 한다.

**룩어헤드/체결 모델**: 엔진이 아니므로 해당 없음. 실현 수익률에 대한 사후 통계라 미래참조 위험은 구조적으로 없다(단 `outliers` 임계값 등 일부는 전체표본 기준).

## 4. 몬테카를로

`_montecarlo.py`는 **과거 수익률을 셔플(`rng.permutation`)** 해 다수 경로를 생성하는 단순 부트스트랩이다(`:274`). numpy 벡터화, `MonteCarloResult` dataclass가 종가치·최대낙폭 분포, bust/goal 확률, 신뢰밴드를 지연 계산으로 제공. **한계: i.i.d. 가정** — 셔플이 시계열 의존성(자기상관·변동성 군집)을 파괴한다. 블록 부트스트랩이 아니므로 단타/추세 전략의 파산확률을 낙관적으로 추정할 수 있다. 참고 지표로만.

## 5. 유지보수 상태 (매우 양호)

활발히 관리됨. pyproject 이관, Python 3.10+ union 타입힌트 전면 적용, ruff+pyright, 125개 테스트 통과, `_compat` 레이어로 pandas 1.5~2.2/numpy 2.0 호환. Apache-2.0(**talon 벤더링·수정 자유**). de-facto 표준급 티어시트 라이브러리. 코드가 작고(8k LOC) 초점이 명확해 읽고 이식하기 쉽다.

## 6. talon 적용성 평가

### 그대로 채택 (리포팅 레이어에 한정)
- talon **자체 백테스트/페이퍼 엔진이 산출한 equity curve → 수익률 Series**를 quantstats에 넣어 Sharpe/Sortino/Calmar/MDD/VaR/CVaR/Kelly/drawdown_details를 계산하고 `reports.html()`로 티어시트를 생성. 사용자 최종판단 워크플로(텔레그램/PC 리뷰)에 이상적인 산출물.
- `qs.stats.*`를 개별 호출해 talon 내부 지표 대시보드/알림에 재사용.

### 패턴만 빌릴 것
- `_prepare_returns`류 **단일 정규화 게이트웨이** 패턴, `drawdown_details`·`autocorr_penalty`·PSR 등 공식 구현을 참고.
- HTML 티어시트를 **LLM+사람 공용 리뷰 표면**으로 삼는 아이디어.

### 참고/피할 것
- yfinance 데이터 경로(`download_returns`, `_prepare_benchmark`) — **사용 금지**, 토스/KRX 자체 파이프라인으로 대체.
- 기간 단위 승률·손익비 — 단타 트레이드 평가에 **쓰지 말 것**. 트레이드 단위는 talon 자체 구현.
- 모수적(정규분포) VaR과 i.i.d. 몬테카를로 — 한국 소형주·단타의 팻테일을 과소평가. 리스크 사이징 근거로 맹신 금지, 히스토리컬/블록 방식 병행.
- `extend_pandas()` 전역 몽키패치 — 명시 호출 선호.

### 한국 시장 적용성
지표 자체는 **시장 무관**(수익률만 받음)이라 KOSPI/KOSDAQ·미장 모두 동작. `periods_per_year`가 인자로 노출되어 KRX(연 약 245거래일)도 값만 넘기면 됨(기본 252도 실무상 무방). 벤치마크는 KOSPI/S&P500 수익률 Series를 직접 주입. 티어시트 라벨이 영어·USD 지향이나 표면적 이슈. **일중(단타) 데이터는 일별 수익률 지향 설계와 맞지 않음** — 트레이드별/일별 PnL로 리샘플해 넣어야 함.

### 구독제 LLM 제약과의 궁합 (탁월)
quantstats는 **LLM/외부 API 의존이 전혀 없는 순수 수치 계산**이다(네트워크는 yfinance뿐, talon은 이를 우회). 종량 과금 리스크 0. 게다가 그 산출물(지표 dict/DataFrame, HTML 티어시트)은 **결정론적·압축적 아티팩트**라, Claude Max 구독(Claude Code/Agent SDK) 에이전트에 "이 전략의 지표다, 매매 근거를 제시하라"는 식으로 컨텍스트로 넘기기에 최적이다. 즉 quantstats가 무료로 숫자를 만들고, 구독 LLM은 그 숫자를 해석·설명하는 역할 분담이 깔끔하게 성립한다.

## 7. 결론

**Verdict: borrow.** quantstats는 talon의 엔진이 될 수 없지만, **성과·리스크 리포팅 레이어로는 그대로 채택**할 가치가 큰 성숙한 라이브러리다. 백테스트/체결/데이터 파이프라인은 talon이 별도 구축하고, 그 결과 수익률을 quantstats에 흘려 지표·티어시트를 얻는 구조가 최적. 단타 트레이드 단위 통계와 팻테일 리스크는 자체 보강이 필요하다.
