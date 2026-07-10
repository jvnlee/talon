# backtrader 정밀 분석 (talon 설계 관점)

- 레포: https://github.com/mementum/backtrader (mementum / Daniel Rodriguez)
- 분석 버전: 1.9.78.123, 최종 커밋 2023-04-19 (실질적으로 유지보수 정체)
- 규모: 약 35,000 LOC, 순수 Python. 라이선스: **GPL v3 (카피레프트)**
- 성격: 이벤트 기반 단일/멀티 종목 백테스팅 + 라이브 트레이딩 프레임워크

---

## 1. 핵심 아키텍처

### 1.1 "Lines" 데이터 모델 (핵심 발명)
전체 시스템이 `LineBuffer` 추상화 위에 서 있다. 각 라인(가격 시계열, 지표 출력)은
`array.array` 기반 버퍼이며 **인덱스 0 = 현재 바, 양수 = 과거, 음수 = 미래(확장 시)**.
덕분에 `self.data.close[0]`(현재 종가), `[-1]`(직전 종가)처럼 시점 이동 로직 없이
"현재"만 읽으면 된다(`linebuffer.py`). 라이브용 링버퍼 모드(`QBuffer`)로 메모리 상한도 지원.

### 1.2 메타클래스 DSL
`metabase.MetaParams` / `AutoInfoClass` / `LineSeries` 계층이 클래스 선언을
런타임에 재작성한다. 지표는 `lines = ('sma',)`, `params = (('period', 30),)`만
선언하면 접근자·파라미터·플롯 메타가 자동 생성된다. 표현력은 높지만 **메타프로그래밍이
과도**해 디버깅·타입힌트·IDE 추론이 어렵고, Python 2/3 호환 잔재(`with_metaclass`,
`utils/py3.py`)가 코드 전반을 오염시킨다.

### 1.3 Cerebro 엔진과 이중 실행 모드
`cerebro.py`(63K LOC)가 오케스트레이터다. 두 실행 경로가 공존한다:
- `_runnext`: **이벤트 기반**. 바가 도착할 때마다 모든 지표·전략·옵저버의 `next()` 호출.
- `_runonce`: **벡터화**. 지표는 `once(start, end)` 루프로 배열을 한 번에 계산(백테스트 가속).
  단 전략·브로커·옵저버는 항상 이벤트 방식(`_oncepost`)으로 유지 → 룩어헤드 방지.

모든 지표가 `next()`와 `once()`를 **동시에 구현**한다(`basicops.py`, `crossover.py` 참고).
같은 전략 코드가 백테스트에서는 벡터화로 빠르게, 라이브에서는 이벤트로 정확하게 돈다.
**이 "단일 코드, 이중 실행" 설계가 backtrader의 가장 배울 만한 지점이다.**

---

## 2. 데이터 파이프라인

`feed.DataBase._load()`가 바 단위로 라인을 채운다. 지원 소스: CSV/제네릭CSV, **Pandas
DataFrame**, Yahoo, Quandl, IB, Oanda, InfluxDB, Blaze, VisualChart, SierraChart, MT4.
`resamplerfilter.py`의 `Resampler`/`Replayer`가 틱→분→일→주→월 **멀티 타임프레임 리샘플링/리플레이**를
제공한다(단타의 멀티프레임 컨텍스트에 유효). 필터 파이프라인(Heikin-Ashi, Renko, 세션 필터)도 첨부 가능.
`tradingcal.py`는 거래소 캘린더 추상화(`PandasMarketCalendar`)를 제공하나 **KRX 캘린더는 없음**.

---

## 3. 백테스트 엔진 설계 (체결 · 비용 · 룩어헤드)

`brokers/bbroker.py`가 시뮬레이션 브로커다. 설계 품질이 높다:

- **체결 모델**: Market은 **다음 바의 시가**에 체결(생성 시점 이후만 → 룩어헤드 원천 차단).
  Limit/Stop/StopLimit/Close/Trailing은 바 OHLC를 검사(`_try_exec_*`). Close는 세션 종료 감지.
- **슬리피지**: 퍼센트/고정 두 방식 + `slip_match/slip_limit/slip_out` 옵션으로
  "체결 불가 시 최선가로 매칭 vs 미체결" 정책 선택(`_slip_up/_slip_down`).
- **수수료**: `comminfo.py`가 퍼센트/고정, 선물 마진·승수·레버리지, 보유 이자(credit interest)까지 모델링.
  한국 주식의 위탁수수료+거래세+농특세 구조도 파라미터로 재현 가능(유연함).
- **룩어헤드 방지 2중 장치**: (1) 시장가는 다음 바 체결, (2) **minperiod 워밍업** —
  지표의 `_minperiod`를 데이터·하위지표에서 전파 계산하고 `prenext()`(워밍업)→`nextstart()`(경계
  1회)→`next()`로 전이(`lineiterator.py`). 데이터가 충분히 쌓이기 전엔 값을 내지 않아
  **시계열 point-in-time 정합성**이 보장된다.
- 보조: `cheat_on_open`/`cheat_on_close`(의도적 룩어헤드 테스트), 브래킷/OCO 주문.

---

## 4. 리스크 관리 · 사이징

빈약하다. `sizers/`에는 `FixedSize`, `PercentSizer`뿐이고, `order_target_percent/size/value`로
목표 비중 리밸런싱은 가능하나 **포트폴리오 레벨 리스크(VaR, 섹터·종목 한도, 변동성 타깃팅,
켈리)는 전무**. 리스크 통제는 전략 코드에서 직접 짜야 한다.

---

## 5. 라이브 트레이딩 어댑터

`Store`(싱글턴) + `Broker` + `Data` 삼각 구조가 깔끔하다(`store.py`). 어댑터를 하나 쓰면
동일 전략을 백테스트/라이브에서 재사용한다. 그러나 **구현체가 모두 사망 상태**:
IB는 `ib.ext`(IbPy, Python2 시대 데드 라이브러리), Oanda는 구형 v1 REST, VisualChart는 Windows COM.
스레드+deque 알림 패턴은 참고할 만하나 코드 그대로는 못 쓴다.

---

## 6. 퀀트 코어 평가 (talon 임무 핵심)

- **팩터/알파 표현**: 연산자 오버로딩으로 `self.data.close > sma`, `sma1 - sma2`, `And(a, b)`가
  지연 평가 `LineBuffer`로 합성된다. 표현력은 좋지만 **전적으로 종목별 시계열 연산**이다.
  qlib/zipline+alphalens류의 **횡단면(cross-sectional) 알파 팩터 표현식 엔진·랭킹·IC 분석은 없다.**
- **ML 통합**: **없음.** sklearn/torch/xgboost 의존성 0. 가장 근접한 게 `indicators/ols.py`인데
  `statsmodels.OLS`를 **매 바마다** 재적합(느림)하고, `OLS_BetaN`은 pandas 0.20(2017)에서
  제거된 `pd.ols`를 호출 → **죽은 코드**. 방치 상태의 방증.
- **Point-in-time**: 시계열 워밍업(minperiod) 차원에서는 견고하나, PIT 데이터베이스·생존편향
  보정·재무데이터 시점 관리 개념은 없다(가격 OHLCV 중심 프레임워크).
- **성과 분석**: `analyzers/`는 충실 — Sharpe, DrawDown, SQN, Calmar, VWR, TradeAnalyzer,
  Returns, PyFolio 연동 등. 플러그인 패턴(`Analyzer` 서브클래스)이 talon에 이식 가치가 높다.

---

## 7. 유지보수 상태

최종 릴리스 2023-04이나 실질 개발은 수년째 정체(원저자 비활동, 커뮤니티 소규모 PR 트리클).
setup.py 분류자가 Python 3.7에서 멈춤. Python2 호환 잔재가 상존. GPLv3라 **talon을 외부
배포·공유하면 카피레프트가 전이**된다(개인 비배포 사용은 무방하나 확장 지향과는 상충).

---

## 8. talon 적용성 판정

**결론: 코드 채택(X) — 패턴 차용/참고(O).** 이유: (a) GPLv3 카피레프트, (b) 메타클래스 마법과
Python2 잔재로 확장·디버깅 비용 큼, (c) 한국 시장·데드 어댑터, (d) 퀀트 팩터/ML 코어 부재.

**차용할 설계 패턴 (talon 자체 엔진에 이식):**
1. Lines 인덱스-0 시계열 추상화 — 지표/전략 로직을 시점 이동 없이 표현.
2. **단일 코드 · 이중 실행(next/once)** — 백테스트 벡터화 + 라이브 이벤트 동일 로직.
3. **minperiod 워밍업**에 의한 룩어헤드 차단(prenext→nextstart→next).
4. 이벤트 기반 체결 모델(시장가=다음 바 시가) + 슬리피지/수수료 정책 분리.
5. Store/Broker/Data 어댑터 삼각 구조 — 백테스트/페이퍼/실전 무변경 스위칭.
6. Analyzer 플러그인 패턴 — 성과·리스크 지표 모듈화.

**한국 시장 적용성**: 토스증권 OpenAPI용 `Store`/`Feed`/`Broker`를 신규 구현해야 하며,
KRX 거래 캘린더, 상·하한가(±30%) 제약, 호가단위(틱사이즈), 원화 수수료+거래세 모델을
추가해야 한다. 수수료 클래스는 유연해 재현 가능. **단타의 현실적 체결에는 backtrader의
바 OHLC 체결이 조악** — 호가창/틱 기반 체결이 필요하면 이 엔진으로는 부족하다(분봉 스윙엔 무난).

**구독제 LLM 제약과의 궁합 (양호)**: backtrader는 순수 수치 계산으로 **LLM과 완전 분리**된다.
LLM(Claude Code/Agent SDK)이 리서치·시그널 제안·매매근거 생성을 담당하는 **상위 판단층**,
backtrader류 엔진은 그 아래 **결정론적 백테스트/실행 커널**로 두는 계층 분리가 자연스럽다.
API 종량과금이 전혀 없어 구독제 제약과 충돌하지 않는다. 다만 backtrader 자체엔 LLM 훅이 없으므로
talon이 시그널 주입 인터페이스(예: 외부 스코어를 데이터 라인으로 피드)를 설계해야 한다.

**피할 것**: 메타클래스 과용 아키텍처 그대로 모방(가독성·타입안정성 저하), GPLv3 코드 직접 포함,
데드 라이브 어댑터 재활용, ML 팩터 백테스트를 이 엔진에 기대는 것.

**최종 권고**: backtrader는 **이벤트 기반 단일종목 백테스트 시맨틱의 교과서적 레퍼런스**로 삼되,
talon 코어는 위 6개 패턴을 흡수한 **경량 자체 엔진**(또는 활발히 유지되는 현대 라이브러리)으로
구축하고, 횡단면 팩터/ML은 별도 리서치 파이프라인으로 분리하는 것이 바람직하다.
