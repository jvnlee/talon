<!-- 생성: 2026-07-18. 방법: microsoft/qlib 소스(2026-04 HEAD, 파이썬 5.6만 줄)를 Opus 4.8 서브에이전트 32개 워크플로로 감사 — 15개 하위시스템 정독(expression·data·handler·alpha·backtest·strategy·eval·model·workflow·online-drift·pit·high-freq·weight-strategy·region-config·calendar) + 후보별 talon 적합성 적대 판정 + 누락 점검. 판정(adopt/adapt/idea_only/reject·우선순위·작업량)은 심판 결론을 그대로 반영함. -->

# qlib 심화 흡수 리서치 — 무엇을 이식하고 무엇을 버릴 것인가

> 조사일: 2026-07-18(토)
> 대상: microsoft/qlib(MIT) 소스 15개 하위시스템 정독 + talon 이식 판정
> 관계: 이 문서는 `docs/research/quant-01-qlib.md`(2026-07-11, 얕은 개요 스캔)를 **심화·대체**한다. 그 노트가 "패턴만 빌려라"라고 방향만 제시했다면, 이 문서는 소스 라인 단위로 파고들어 **어떤 후보가 이미 talon에 (대개 더 엄격하게) 구현돼 있고, 어떤 것만 진짜 새 것인지, 언제 착수할지**를 판정한다. 개요 재서술은 하지 않는다.

---

## 0. 한 줄 결론

**qlib에서 통째로 채택할 것은 없고 대부분은 talon이 이미 만들었다(종종 더 엄격하게). 진짜 새로 얻는 것은 (a) 지금 착수 가능한 데이터 품질 점검 2건과 (b) 전략 v2가 열릴 때 붙일 소수의 팩터 연산자·평가지표·체결 정밀화 묶음뿐이다. 착수 시점은 사용자가 정한다 — 현재의 데이터 우선 단계를 앞지르면 안 된다.**

---

## 용어 풀이 (외래어·전문어, 한 줄씩)

- **팩터(factor)**: 종목을 점수 매기는 계산식(예: 20일 모멘텀). talon은 문자열이 아니라 안전 파싱된 표현식으로 정의.
- **연산자(operator)**: 팩터를 조립하는 부품 함수(Mean, Std, Ref 등).
- **cross-sectional(횡단면)**: 같은 날 여러 종목을 서로 비교(그날 전 종목 순위 등). 반대는 시계열(한 종목의 시간 흐름).
- **rolling window(롤링 윈도)**: 최근 N일 구간을 미끄러뜨리며 계산(20일 평균 등).
- **warmup(워밍업)**: 롤링 계산이 온전해지려면 앞쪽에 필요한 과거 봉 수. 이만큼 미리 불러와야 첫날 값이 반쪽이 안 됨.
- **lookahead(룩어헤드)**: 미래 정보가 과거 판단에 새는 것. talon 제1원칙은 이걸 원천 차단하는 것.
- **min_periods**: 윈도가 아직 안 찼을 때의 처리. qlib=반쪽이라도 값(=1), talon=꽉 찰 때까지 null(안전).
- **PIT(point-in-time, 시점 정합)**: "그 시점에 실제로 알 수 있던 값"만 쓰기. 재무·공시 데이터의 발표 지연/정정 누수를 막음.
- **IC / RankIC / ICIR**: 팩터 점수와 다음 수익의 상관(IC=피어슨, RankIC=순위 상관), ICIR=그 상관의 평균÷표준편차(신뢰도 t값 비슷한 지표).
- **DSR(deflated Sharpe ratio)**: 여러 번 시도해서 얻은 샤프를 "몇 번 찔러봤나"로 할인해 과최적화를 걸러내는 지표. talon Gate-1의 핵심.
- **OOS(out-of-sample)**: 개발·튜닝에 절대 안 쓰고 봉인해 둔 검증 구간. talon은 2024-01-01부터 봉인.
- **min-MAD z-score / robust z-score**: 이상치에 강한 표준화(중앙값·중위절대편차 기반). ±30% 상한가 같은 팻테일에 적합.
- **CSRank**: 그날 종목들을 백분위 순위로 바꾸는 횡단면 연산자. talon이 이미 보유.
- **Slope / Rsquare / Resi**: 시간축에 대한 회귀의 기울기·설명력(추세의 매끈함)·마지막 잔차.
- **RSV / %K(스토캐스틱)**: 최근 N일 범위 안에서 지금 종가가 어디쯤인지(0~1). 상하한가에 강한 유계 신호.
- **KBAR**: 하루 캔들 모양 비율(몸통·꼬리·종가 위치) 9종.
- **WMA / EMA**: 가중이동평균 / 지수이동평균.
- **VWAP / TWAP**: 거래량가중평균가 / 시간가중평균가(체결 기준가).
- **slippage(슬리피지)**: 예상가와 실제 체결가의 차이.
- **participation cap(참여도 상한)**: 하루 거래량의 일정 비율까지만 체결됐다고 가정하는 현실 제약.
- **permutation null(순열 귀무 검정)**: 점수↔종목 짝을 무작위로 섞어 "무작위 선택 대비 진짜 엣지인지" 백분위로 검정.
- **walk-forward**: 구간을 앞으로 밀며 재적합·재평가하는 방식.
- **GBDT / LightGBM**: 그래디언트 부스팅 결정트리(테이블형 데이터의 사실상 표준 ML).
- **DoubleEnsemble / NNLS**: 샘플 재가중+피처 선택 앙상블 / 비음(0 이상) 계수 선형회귀.
- **feature importance**: 모델이 어느 피처에 의존하는지의 순위.
- **VI(volatility interruption)**: 장중 변동성 완화 단일가 발동(2~10분). 일봉 거래량 0으로 안 잡힘.
- **closing auction(종가 동시호가)**: 15:20~15:30 단일가 매매. KRX 일봉 종가 = 이 청산가.
- **polars / `.over(...)`**: talon이 쓰는 데이터프레임 라이브러리 / 그룹별 윈도 연산.
- **eval()**: 문자열을 코드로 실행하는 함수. ADR 0005가 팩터 표현식에서 금지.

---

## 1. 요약 표 — adapt 판정 후보 (adopt는 0건)

심판 15명 중 **누구도 "그대로 채택(adopt)"을 주지 않았다.** 아래는 판정이 **adapt**인 것만 모아, 15개 감사에 중복 등장한 동일 작업을 하나의 항목으로 통합한 것이다(괄호 안은 원 감사 출처). idea_only/reject는 5장·부록에서 다룬다.

### 지금(now) — 데이터 우선 단계와 충돌 없음

| 작업 | 판정 | 우선 | 작업량 | 한 줄 근거 |
|---|---|---|---|---|
| **W1. `talon health` 통합 KR-튜닝 데이터 QC 명령** (data-infra c4 + eval-report c9) | adapt | now | S | QC가 limits/crosscheck/reconcile로 흩어져 있고 통합 점검이 없다. 제헌절류 오정지·팩터 드리프트·커버리지 공백(예: `*_1510` 컬럼이 2026-07-15 이전엔 폴백)을 잡는 단일 패스. 데이터 우선 단계에 정확히 부합. |
| **W2. 트라이얼 재현성 캡처(git HEAD/diff, argv)** (workflow-exp c3) | adapt | now | S | 트라이얼 레지스트리가 params 문자열만 저장하고 "어떤 코드가 이 숫자를 냈나"는 없다. IS 재튜닝 사이클을 반복하는 프로젝트에서 승격 결과를 몇 달 뒤 되짚을 때 필수. seed 캡처는 결정론 코어라 사문(死文)이므로 제외. |

### 다음(next) — 전략 v2 / 엔진 하드닝 슬롯. 현재 ACTIVE_STRATEGIES 비어 있어 지금은 착수 대상 아님

| 작업 | 판정 | 우선 | 작업량 | 한 줄 근거 |
|---|---|---|---|---|
| **W3. v2 팩터 연산자 + zero-variance NaN 가드** (expr-ops c3/c7/c9, alpha operator/trend) | adapt | next | M | 진짜 빠진 것: Corr/Cov, Slope/Rsquare/Resi, WMA, Quantile, 롤링 TSRank, Skew/Kurt. `_pair_window_warmup` 골격이 미사용 대기 중. polars로만 이식(numpy/Cython 금지, ADR 0010). Corr/Rsquare는 상하한가·VI로 분산 0인 윈도에서 ±1/inf가 나므로 **상대 오차 기준** NaN 가드 필수. |
| **W4. 레인지-포지션·RSV·트렌드품질 팩터 가설** (alpha 범위/캔들/RSI) | adapt | next | M | RSV/RANK은 유계[0,1]라 ±30% 이상치에 강하고, "N일 저점 근처 매수 vs 신고가 돌파"는 close-bet v1의 스파이커 추종과 **다른** 오버나이트 가설. Rsquare는 매끈한 추세와 급등 스파이크를 가른다. W3 위에 얹힘. DSR 트라이얼 예산을 이 계열이 선도. |
| **W5. 횡단면 정규화 스테이지 + CSRobustZScore** (expr-ops c6, handler c1) | adapt | next | S | 스테이지 분리는 이미 구조적으로 강제됨. 추가분은 CS 로버스트 z(중앙값+1.4826·MAD, ±3 클립) 연산자 — KRX 팻테일에서 평범한 z보다 적합. |
| **W6. inf/null 처리 순서·"채우지 말고 제외"** (handler c8) | adapt | next | S | factor-engine-notes 미해결 #4(Log/0나눗셈 inf 전파)에 정확히 대응. ±inf를 스코어링 전에 제거, 정규화 **후** 채움, 그리고 폭주 행은 채워 넣기보다 적격 마스크로 **제외**. |
| **W7. 엔진에 정밀 상하한가 플래그 배선(direction-aware)** (backtest c1, strategy c3, region c2) | adapt | next | S | 패널은 이미 틱-그리드 정확 limit_up/down/touch 플래그를 실지만, 엔진 체결 게이트는 아직 float 0.295로 재유도한다. 정본 플래그를 종가 매수 거부 경로에 배선(ex-rights 기준가일 오분류 제거). 익일 시가 매도 게이트는 open-limit 판정이 별도 필요. |
| **W8. 종가 동시호가 거래량 기반 체결 캡** (backtest c6, strategy c5, region c8) | adapt | next | M | 진입 캡(일봉 거래량 10%)은 있으나, 종가 체결은 하루의 작은 일부인 동시호가 물량에서 청산된다(005930 실측 ~11%, 중앙값 훨씬 낮음). 종가 체결 캡 기준을 종가 동시호가 물량으로 전환. 청산 레그에도 대칭 캡 추가. |
| **W9. 독립 손익 정합성 체크** (backtest c5) | adapt | next | S | 스키마·벤치마크는 중복. 진짜 값은 체결(trades) 합산 손익-수수료를 equity 곡선 델타와 **독립 대조**하는 단정문 — Gate-1 전에 이중과금·로트 유실을 잡는다. |
| **W10. 15:10 결정가 vs 종가 체결가 이원화 + 슬리피지 캘리브레이션 + price_advantage(bps)** (highfreq c3/c4, weight c1) | adapt | next | M | 결정은 close_1510, 체결은 종가 청산가로 이미 분리돼 있다. 추가분: 트레이드별 decision_price/fill_price 별도 기록, 평면 0.1% 슬리피지를 15:10↔15:35 스냅샷으로 유동성 티어별 캘리브레이션, 결정가 기준 implementation-shortfall(bps) 텔레메트리. |
| **W11. 15:10 세션 누적 참여도·강도 팩터** (highfreq c2) | adapt | next | M | "15:10까지 유독 활발" 신호. `volume_1510 / Ref(Mean(volume_1510,20),1)` 등 한 줄 팩터로. 정확 `volume_1510`은 2026-07-15부터만 축적(백필 불가), 일봉 근사 구간에선 무의미 — 데이터 게이트됨. |
| **W12. 크로스섹셔널 IC/RankIC/ICIR + 롱온리 적중률·롱애버리지** (eval c1/c3) | adapt | next | S | 코드에 IC 자체가 없다. DSR(손익 기반)·cohort(불리언 신호)와 직교하는 랭크-점수 진단. `.corr` 그대로 이식 금지(상수 벡터 NaN 팽창) — RankIC 우선, 유효일 분모 명시. cohort.py의 baseline/Welch-t 재사용. |
| **W13. 순열 귀무 검정(permutation null)** (workflow c6) | adapt | next | M | 그날 유니버스 안에서 점수↔종목을 섞어 "무작위 픽 대비 오버나이트 손익이 구별되는가"를 백분위로. close-bet v1의 "상위3=엣지0 스파이커"를 더 빨리 잡았을 검정. DSR·cohort와 별개의 제3 검정. IS 한정. |
| **W14. 팩터 신뢰 IC 감쇠·부호반전 모니터** (online-drift c4) | adapt | next | M | 팩터별 롤링 RankIC를 유지, 추세 IC가 in-sample IC의 k배 미만이면 축소, N일 부호 반전이면 보류 — **축소/보류만** 하는 결정론 하드 게이트(ADR 0006·0002 부합). SURVIVAL_MIN_N을 최소 breadth로 재사용, 상하한 종목 제외. 배포된 팩터가 있어야 물릴 수 있어 페이퍼 단계 대상. |
| **W15. 워크포워드 윈도 생성기 + OOS 스티칭(IS 한정)** (online-drift c1/c5) | adapt | next | M | evaluate.py는 단일 고정 분할뿐. 캘린더 인덱스 시프트 + trunc_days=horizon+1 누수 가드로 IS 내부 재적합/강건성 윈도 생성 후, 구간별 OOS 예측을 하나의 곡선으로 이어 붙여 정직한 워크포워드 곡선. **봉인된 2024-01-01 경계를 넘으면 안 됨**(ADR 0011). |
| **W16. 팩터 중요도 트리아지(자문용)** (models-bench c2) | adapt | next | S | IS에서 LightGBM/permutation-importance로 팩터를 순위 매겨, 낮은 팩터가 트라이얼 예산을 먹기 전에 걸러냄. **중요도≠엣지**(v1 실패의 핵심)이므로 cohort의 순비용 엣지 검정에 **종속된 자문 신호**로만, 절대 하드 선택기 금지. |
| **W17. 캘린더 리인덱스 + 명시적 정지 NaN** (data-infra c5) | adapt | next | M | 캘린더 권위(XKRX)는 완비됐으나 종목별 리인덱스는 미적용 — 정지일이 그냥 없는 행이라 polars 롤링이 캘린더 갭을 압축(Mean(vol,20)이 20 거래일 초과 span 가능). 짧은 윈도·유동주엔 드묾. 캘린더-길이 민감 팩터가 도입될 때(v2) 팩터 정확성 보험으로. halt 마커 필요. |
| **W18. 전체 run 설정 직렬화 + 트라이얼 설정 해시** (workflow c1, region c5) | adapt | next | S | 안전 레지스트리(STRATEGY_FACTORIES)는 이미 있음. 추가분: gate/regime/cost/universe를 하나의 재현 가능한 dict로 직렬화하고 EngineConfig+KrCostModel 해시를 트라이얼 행에 기록 — 비용 가정이 바뀌어도 DSR 트라이얼 카운트가 오염되지 않게. |

### 나중(later) — adapt이나 선행 조건/트랙이 아직 없음

| 작업 | 판정 | 우선 | 작업량 | 한 줄 근거 |
|---|---|---|---|---|
| **W19. 3소스 크로스체크 정수-정확 강화** (data-infra c3) | adapt | later | S | reconcile/crosscheck는 이미 가동. 잔여: volume/value 같은 카운트 필드를 상대오차 대신 정수-정확 비교로, "정본 덮어쓰기가 기대 필드만 건드렸나" 단정 추가. |
| **W20. DART PIT 관측일 질의 + 누적→분기 차분 + 엄격 T-1 게이팅** (expr-ops c5, pit-fundamentals c1·핵심 인사이트) | adapt | later | M | 펀더멘털 값 수집기가 아직 없다(현재 DART는 공시 메타만). 착수 시 규칙: 리비전 보존, DART 분기=YTD 누적이라 TTM/QoQ 전 **차분 필수**, list API가 날짜 단위라 당일 공시는 **T-1로 취급**(장중 시각 없음). 오버나이트 스윙이 아니라 스윙 트랙용. |
| **W21. "테이블형=GBDT" 프레임을 ADR 0005/A18에 흡수** (models-bench c1) | adapt | later | S | Alpha158(엔지니어드 테이블형)에서 GBDT/DoubleEnsemble이 모든 신경망을 이김; 신경망은 원시 시퀀스에서만·더 낮은 절대수익. talon은 분봉·그래프 데이터가 없어 신경망 동물원은 이중으로 무관. 새 문서 말고 ADR 0005/SYNTHESIS A18에 한 줄 병합, Phase 5+로 스탬프. |
| **W22. 시그널 오토코렐레이션·이름 오버랩 용량 진단** (eval-report c7) | adapt | later | S | fidelity.py의 top-K Jaccard 오버랩을 하루-대-하루 랙으로 재사용. 같은 (소형·비유동) 종목을 반복 픽하는지 = 1000만원 시드 용량 한계 진단. 해석용, go/no-go 아님. v2 점수·엣지가 있어야 유효. |

---

## 2. 주제별 상세

각 항목: **무엇 → qlib 위치 → 왜 가치 → talon 이식 방법 → 주의점 → 판정·이유.** 이미 구현된 것은 "왜 reject/idea_only인지"를 밝힌다.

### 2.1 팩터 가설 (factor hypotheses)

**핵심 사실: talon은 이미 qlib 파생 팩터 엔진을 보유**(`src/talon/factors/{parser,expr,ops,engine}.py`, `docs/research/factor-engine-notes.md`). eval-없는 AST 화이트리스트 파서, warmup 부기, 음수 시차(미래 참조) 파스타임 금지, CSRank, null-until-full 정책이 전부 들어가 있고 **qlib보다 엄격**하다. 그래서 팩터 관련 대다수 후보는 중복.

**W3 — v2 연산자 세트** (add: Corr/Cov, Slope/Rsquare/Resi, WMA, Quantile, 롤링 TSRank, Skew/Kurt)
- qlib 위치: `qlib/data/ops.py:713`(Rolling), `:1387`(PairRolling), `:1467`(Corr, zero-std 가드 `:1494`), `qlib/data/_libs/rolling.pyx:48`(스트리밍 회귀), `qlib/contrib/data/loader.py`.
- 왜: 표준 알파 어휘의 꼬리 부분. 흐름 지속성(`Corr($inst_net,$ret,N)`), 추세 품질(`Slope($close,N)/$close`, `Rsquare`), 관계형 특징을 표현 가능케 함.
- 이식: `_pair_window_warmup` 골격 위에 polars 네이티브 롤링 표현으로. Cython/numpy 금지(ADR 0010). Slope/Rsquare/Resi도 running-sums를 polars 롤링으로.
- 주의점: min_periods=1을 따라오지 말 것(talon은 null-until-full). Corr/Rsquare zero-variance 가드는 qlib의 절대오차 atol=2e-05(정규화 입력용)를 쓰지 말고 KRW 정수가엔 **상대 오차 또는 수익률 기준**. Skew N>=3, Kurt N>=4. N-오버로딩(0=expanding, 0<N<1=ewm)은 버리고 명시 연산자.
- 판정: **adapt / next / M.** v2 어휘라 데이터 우선 단계를 앞지르면 안 되지만, 새 작업 중 최고가치이고 v2가 열리면 첫 번째로 할 것.

**W4 — 레인지-포지션·RSV·트렌드품질·KBAR·RSI 계열** (부록 A 참조)
- qlib 위치: `loader.py:176`(MAX/MIN/QTLU/QTLD/RANK/RSV), `:104`(KBAR 9식), `:149`(ROC/MA/STD/BETA/RSQR/RESI), `:229`(CNTP/SUMP).
- 왜: **엔진 존재 대비 진짜 새 가설은 이 계열 중 range-position/RSV**. 유계[0,1]·상하한 강건·close-bet v1의 스파이커 픽과 구조적으로 다른 오버나이트 가설이며 talon의 "오버나이트 엣지=대형주 집중" 발견과 결합.
- 이식: RSV/RANK/KBAR/CNT/SUMP는 W3 연산자 위 한 줄 표현식. 반드시 T-1 기준(당일 종가·고저·전체거래량은 15:10에 없음, ADR 0013), 또는 스냅샷-앵커 변형.
- 주의점: MAX/MIN 원값은 정확히 ±30% 스파이크가 사는 곳이라 오염 — 유계 RANK/RSV를 선호. KBAR는 상하한 잠김일(O=H=L=C)에 퇴화. 모멘텀/RSI 계열은 v1이 이미 실패시킨 가설을 되밟으므로 range-position에 **종속**시키고 트라이얼 예산을 배분.
- 판정: **adapt / next / M.** KBAR·트렌드·RSI 각각 adapt(next)이나, 스냅샷 KBAR은 정직한 백테스트가 ~3개월 실제 15:10 패널로만 가능(부록 A 주의).

**W16 — 팩터 중요도 트리아지** (models-bench c2)
- qlib 위치: `examples/benchmarks/README.md:75`(LGBM 중요도로 158→20 선별), `qlib/contrib/model/double_ensemble.py:190`(permutation importance).
- 왜: 팩터가 트라이얼 예산을 먹기 전 IS에서 순위 매겨 걸러냄. 리서치 전용이라 매매 결론 무접촉 → ADR-clean.
- 이식: IS에서만 LGBM 적합 후 gain + permutation(한 컬럼 셔플→표준화 손실 증가) 순위. 라벨은 CSRank 정규화.
- 주의점: **중요도≠엣지.** close-bet v1은 예측력 높아 보이는 "일간 상승폭"을 픽해 순비용 엣지 0이었다 — gain 중요도는 바로 그걸 상위로 올렸을 것. 반드시 cohort의 **순비용 엣지 검정에 종속된 자문**으로만, 하드 선택기 절대 금지. OOS 무접촉.
- 판정: **adapt / next / S.**

### 2.2 데이터·PIT

**W1 — `talon health` 통합 QC 명령** (data-infra c4 + eval-report c9) — **now**
- qlib 위치: `scripts/check_data_health.py:136`(대형 스텝), `:185`(팩터 존재), `:79`(대소문자); `qlib/contrib/report/data/ana.py:59`(피처별 NaN/Inf/카디널리티/평균-표준편차 드리프트/오토코렐레이션).
- 왜: **현재 데이터 우선 단계에 정확히 부합하는 유일한 now-후보.** QC가 limits/crosscheck/reconcile/lookahead/fidelity로 흩어져 통합 패스가 없다. 제헌절류 오정지·팩터 드리프트·`*_1510` 폴백 커버리지 공백을 잡았을 것.
- 이식: polars로 (day, symbol) 롱 패널을 컬럼별×일자별 스캔하는 `feature-health`/`health` CLI(JSON 리포트, 기존 관용 일치). KR 튜닝: 조정가 |변동|>0.31=분할/증자/오류, 원가 같은 검출기=순수 분할/증자 탐지, NaN 검사는 "상장·비정지일" 한정, 중복 (day,symbol), XKRX 캘린더 갭, prev_close×[0.7,1.3] 밴드 이탈.
- 주의점: qlib의 0.5가격/3.0거래량 기본값은 US/CN 튜닝이라 오작동. CSI 스킵리스트·소문자-디렉터리 체크(#2053)·np.int는 버림. 서술적 도구지 게이트 아님(해석은 수동). NaN-vs-halt 구분은 W17의 halt 마커 필요.
- 판정: **adapt / now / S.**

**W17 — 캘린더 리인덱스 + 명시적 정지 NaN** (data-infra c5)
- qlib 위치: `scripts/dump_bin.py:227`(data_merge_calendar), `qlib/data/data.py:111·154`.
- 왜: 정지일이 "없는 행"이면 polars 롤링(위치 기반, 캘린더 무관)이 갭을 압축해 Mean(vol,20)이 조용히 20 거래일 초과 span. 캘린더-길이 민감 팩터의 정확성 보험.
- 이식: 캘린더 권위(markets/kr.py, XKRX)는 완비. 종목별 [min,max] 클립 후 캘린더에 리인덱스, 정지=명시 null(전방채움 절대 금지).
- 주의점: 현재 짧은 윈도·유동 상위 종목엔 드물게만 물림 — v2에서 캘린더 길이 민감 팩터가 실전 투입될 때의 보험. halt/holiday/delisting 구분은 halt 마커 필요.
- 판정: **adapt / next / M.**

**W19 — 3소스 크로스체크 정수-정확 강화** (data-infra c3)
- qlib 위치: `scripts/check_dump_bin.py:94`(datacompy abs_tol/rel_tol).
- 왜: talon은 이미 reconcile/crosscheck를 launchd로 가동. 잔여는 volume/value 같은 큰 카운트에 상대오차를 쓰면 오류가 숨는 문제.
- 이식: 카운트 필드는 정수-정확 비교로, "정본 덮어쓰기가 기대 필드만 변경" 단정 추가.
- 판정: **adapt / later / S.**

**W20 — DART PIT** (expr-ops c5 + pit-fundamentals 핵심)
- qlib 위치: `qlib/data/pit.py:24`(P 연산자), `qlib/data/data.py:797`(searchsorted side='right'), `pit.py:33`(미래 기간 참조 금지), `scripts/dump_pit.py:247`(리비전 링크드리스트), `scripts/data_collector/pit/collector.py:227`(발표일 부재 시 +45일 보수 폴백).
- 왜/이식: 시맨틱만 SQLite에 이식(바이너리 .index/.data 포맷은 버림). (관측일)-기준 as-of "리비전 <= 결정시각 중 최신", 미래-기간-참조 가드.
- 주의점(중대): (1) DART list.json은 **접수 날짜만**(장중 시각 없음) — 당일 공시는 investor_flows처럼 **T-1로** 취급해 오후 공시 누수 차단. (2) DART 분기/반기는 **YTD 누적** 흐름값이라 TTM/QoQ 전 **discrete 차분 필수**(자산·자본 같은 stock 항목은 차분 금지). (3) 리비전은 접수번호로 절대 (ticker,period) 붕괴 금지. talon의 polled_at이 실제 관측시각.
- 판정: **adapt / later / M.** 펀더멘털 값 수집기가 아직 없고, DART는 백필 가능해 지금 서두를 이유가 없다(스윙 트랙·데이터 큐 뒤).

**이미 구현돼 reject된 것**: 조정가 factor 규율(data-infra c1 — `data/adjust.py`가 이미 KR FDR 소스 기반, 무상증자일 가짜 상한 가드까지 초과 달성), 워밍업 확장창(c2 — ops.py warmup 콜러블 + engine 전파, 미래참조 파스타임 금지로 qlib보다 강함), PIT 유니버스 멤버십(c6 — stock_info 일자별 스냅샷 + `_info_as_of`가 정적 span보다 우수), upsert/rewrite 쓰기(c8 — store.upsert + reconcile.apply_official). **캘린더 정수 시프트 원시함수 전체(감사 15)도 reject** — KrxCalendar가 exchange_calendars XKRX로 bisect 세션 산술을 이미 내장, `@lru_cache` + `cache_clear()` 리컨사일 무효화까지 동일.

### 2.3 리키지 방어·전처리 (leakage defense / preprocessing)

**여기가 talon이 가장 성숙한 영역.** 감사 3(handler-processor)·감사 15(calendar)는 대부분 talon 자체 방어를 되발견했다: 음수 시차 파스타임 금지(engine.py `_int_param`), `verify_intraday`가 close_overnight 전략의 당일 forming 컬럼 lag-0 참조를 **구성 시점에 하드 거부**(ADR 0013), `verify_factors`/`verify_replay`의 절단-불변 테스트, null-until-full 기본값, 공유 부분식 dedup.

**W6 — inf/null 처리 순서·"채우지 말고 제외"** (handler-processor c8) — 유일한 진짜 gap
- qlib 위치: `qlib/contrib/data/handler.py:41`(순서: ProcessInf→ZScoreNorm→Fillna), `qlib/data/dataset/processor.py:161`(ProcessInf, 자체 'FIXME: very weird').
- 왜: factor-engine-notes 미해결 #4(Log/0나눗셈 inf 전파)에 정확히 대응하는 열린 gap.
- 이식: 스코어링 전 ±inf 제거, 정규화 **후** 채움(정규화 전 원값 0채움은 halt일에 "이 종목 거래대금=0"이라는 허위 주입). qlib의 same-day-mean 대입은 버림(조용한 횡단면 의존, qlib도 '이상하다'고 표기) — 폭주 행은 **적격 마스크로 제외**(더 안전).
- 주의점: signals.py가 null CSRank 점수를 0.0으로 채우는데, 현재 top-N 픽커엔 무해(null이 바닥으로 가라앉아 미픽)하나, 미래 가중합 점수에선 0.5가 중립이라 왜곡. 문서화해 둘 것.
- 판정: **adapt / next / S.**

**idea_only로 보류(전부 미래 ML/전략-v2 트랙)**: fit-window 정규화 규율(c2 — 시계열 스케일러 자체가 0개, CS 랭킹은 창 무관), learn/infer 뷰 분리·is_for_infer 격리(c3 — 15:10 경로는 ML fit/transform이 아님, verify_intraday가 이미 라벨 누수 구조적 차단), flt_data 마스크(c4 — load_panel이 전 행 보존·downstream 적격 필터라 dropna 오염 위험이 애초에 없음), decision-clock 라벨(c5 — ADR 0013 + 이벤트 드리븐 엔진이 이미 더 정밀), forward-window 누수 탐지(c6 — column_min_lags/verify_intraday로 이미 이식됨), RobustZScore(c7 — 랭크 스코어링엔 표면 없음, ±3 클립이 오히려 ±30% 신호를 잘라냄), fit-once pickled transform(c9 — 상태 있는 학습 변환이 0개).

### 2.4 백테스트 리얼리즘

**핵심: talon은 자체 엔진(ADR 0007)을 이미 갖고 대부분을 KR-정확하게 구현.** direction-aware 상하한 게이트, KrCostModel(연도별 매도세 스케줄), fee-aware 현금 제약 사이징, 10% 거래량 참여 캡, 매도-선-매수 결정론 순서, 종가진입/익일시가청산 per-leg 가격, 거부 사유 로깅이 전부 존재.

**W7 — 정밀 상하한 플래그 배선** (backtest c1 + strategy c3 + region c2)
- qlib 위치: `qlib/backtest/exchange.py:273·338`(direction-aware limit_buy/limit_sell/suspended), `:281`(불리언-컬럼 표현식 경로 LT_TP_EXP).
- 왜: 방향 인지 시맨틱은 이미 엔진에 있으나(limit-up=매수만 차단, limit-down=매도만 차단, suspended=양방), 체결 게이트가 아직 float `limit_move_pct=0.295`로 재유도한다(engine.py:205/300). 패널의 틱-그리드 정확 `limit_up_price`는 무시된 채.
- 이식: 매 bar dict에 이미 실려 오는 정본 limit_up 종가 플래그를 종가 매수 거부(`_close_buys`)에 배선. qlib float 대신 talon 정본 플래그(kr_limits.py, ex-rights 기준가·KONEX 15%·틱 통일 era 반영).
- 주의점: talon 플래그는 종가/터치 기반이라 익일 **시가** 매도 거부 경로엔 open-at-limit 플래그가 별도 필요, 또는 float 유지. factor-change/ex-rights일 limit 가격 null인 행엔 명시적 차단-또는-허용 정책.
- 판정: **adapt / next / S.** ACTIVE_STRATEGIES 비어 지금 게이트를 물릴 백테스트가 없다.

**W8 — 종가 동시호가 거래량 기반 체결 캡** (backtest c6 + strategy c5 + region c8)
- qlib 위치: `qlib/backtest/exchange.py:786`(_clip_amount_by_volume), `:886`(2차식 임팩트).
- 왜: 진입 캡(일봉 거래량 10%)은 있으나 종가 체결은 하루의 작은 일부인 동시호가에서 청산(ADR 0013 실측: 005930 ~11%, 중앙값 훨씬 낮음) — 일봉 10% 캡은 동시호가 전체보다 더 "체결"시킬 수 있다.
- 이식: fill_at='close'는 동시호가 물량 기준으로 캡 전환(fidelity.py/15:35 스냅샷 인프라로 측정). 2차식 임팩트항은 **버림**(10M 시드·유동 상위 유니버스·10% 캡이면 무시 가능, 추정 계수보다 스냅샷 캘리브레이션이 정직). 청산 레그에도 대칭 캡.
- 주의점: 동시호가 실현 물량은 마감 후에만 관측·2026-07-15부터 축적 → 10년 백테스트는 lagged 프록시로 근사(같은 날 실현치 사용 금지, ADR 0013 룩어헤드).
- 판정: **adapt / next / M.**

**W9 — 독립 손익 정합성 체크** (backtest c5)
- qlib 위치: `qlib/backtest/account.py:183`(rtn − cost = earning 항등식).
- 왜: 스키마·벤치마크는 중복(equity=cash+Σshares×mark, benchmark.py). 진짜 값은 trades 합산과 equity 델타의 **독립 대조** 단정.
- 이식: talon의 trades/equity 두 테이블에 대한 검증 불변식/테스트로. qlib의 China return_rate 관례·벤치마크 배관은 버림.
- 판정: **adapt / next / S.**

**W10 — 결정가/체결가 이원화 + price_advantage** (highfreq c3/c4 + weight c1)
- qlib 위치: `qlib/rl/order_execution/simulator_simple.py:342`(pa_bps = (fill/base−1)*1e4), `qlib/backtest/exchange.py:494`(방향별 deal price).
- 왜: 결정=close_1510, 체결=종가 청산가로 이미 분리(신호는 결정가, 진입 수익은 체결가). 추가분: 트레이드별 decision/fill 별도 필드, 15:10↔15:35 스냅샷으로 유동성 티어별 슬리피지 캘리브레이션, 결정가 기준 shortfall(bps) 텔레메트리, 포트폴리오 레벨 헤어컷 버퍼(15:10 사이징가와 동시호가 체결가 드리프트 흡수).
- 주의점: qlib NestedExecutor/SAOE/TWAP 분할 체결은 **전량 버림**(talon은 동시호가 1회 체결, 분봉 없음). weight c1의 균등-현금 사이징 base는 ADR 0006 R-사이징과 충돌하므로 **추가 제약(헤어컷)으로만**, 사이징 base 대체 금지.
- 판정: **adapt / next / M.** 페이퍼/전략-v2 캘리브레이션.

**reject된 것**: 비용 모델 min-fee+critical-price(c2 — KrCostModel이 이미 KR 매도세 비대칭, min_cost는 KR 무의미), 결정-on-past 타이밍(strategy c2/weight c4 — ADR 0013 + verify_intraday로 이미 더 엄격), order/decision per-leg 가격(c7 — fill_at으로 이미 해결), 현금-제약 사이징 critical price(weight c8 — _fill_buy가 이미 fee-shrink 루프). **T+2 현금 지연(c4)은 메커니즘 자체를 폐기** — KRX는 매도대금 당일 재사용 허용이라 이 제약을 넣으면 오히려 실전과 괴리(회전율 허위 축소).

### 2.5 전략·회전율

**핵심: talon은 target-weight 리밸런서가 아니라 이벤트 드리븐 손절/목표/청산 엔진 + 빈 슬롯 채움.** 리스크 게이트가 이미 보유 종목 재매수를 차단하므로 종목이 랭킹 강등으로 청산되는 일이 없다. target-weight 계열(감사 6·13) 대부분은 talon이 안 굴리는 롤링/부분보유 북을 위한 인프라라 idea_only/reject.

- **W7·W8은 위 백테스트 절에서 다룸**(전략 감사에도 중복 등장).
- **reject**: Signal→Order 경계(strategy c2 — QuantCore precompute + MarketView 읽기 전용 + verify_intraday로 이미 구조적 이식), 균등-현금 사이징(weight c6 — ADR 0006 R-사이징과 정면 충돌, CONTEXT가 "균등 비중"을 회피어로 명시, 게다가 +21% 스파이커에 동일 자본 = v1 실패 재현).
- **idea_only**: TopkDropout 히스테리시스(strategy c1 — talon 북이 이미 더 sticky, 순수 오버나이트는 100% 회전 강제라 무의미), target-weight 조기 청산 예외(weight c2 — 익일 하한가 매도불가 유지가 ADR 0012로 이미 모델링), order-diff 엔진(weight c3 — 이벤트 드리븐 discrete-order라 target diff 대상 없음), 회전율 하드 제약+graceful degradation(strategy c6/weight c7 — 리스크 게이트 trim/block 캐스케이드가 이미 결정론적 열화, cvxpy 옵티마이저는 ADR 0005/0010과 충돌), 최소보유일(weight c9 — 순수 close-bet과 상충).

### 2.6 평가지표·리포트

**W12 — 크로스섹셔널 IC/RankIC/ICIR + 롱온리 적중률·롱애버리지** (eval c1/c3)
- qlib 위치: `qlib/contrib/eva/alpha.py:160`(calc_ic), `:71`(long-short/long-avg/precision), `qlib/workflow/record_temp.py:323`.
- 왜: 코드에 IC/상관이 전무. DSR(손익 기반)·cohort(불리언 신호 멤버십)와 **직교**하는 랭크-점수 상류 진단 — 랭크는 맞는데 top-K 컷을 못 넘는 팩터를 잡는다.
- 이식: polars로 groupby-day spearman/pearson, RankIC 우선. 롱온리는 qlib의 (r_long−r_short)/2 숏 레그를 버리고 long-average(top-K − 유니버스 평균)·적중률만. cohort.py의 baseline/Welch-t 재사용.
- 주의점: `.corr` 그대로 이식 금지 — 상하한 잠김일 상수 벡터가 NaN을 조용히 떨궈 ICIR 분모를 부풀린다. 유효일 분모 명시, NaN일 로깅, XKRX 캘린더로 랙. 라벨은 close_t→익일 시가로 **shift 유도**(패널 overnight_ret는 day t로 들어오는 갭이라 그대로 쓰면 부정확). 10년 구간 IC는 `*_1510`이 폴백이라 낙관적 상한.
- 판정: **adapt / next / S.**

**W13 — 순열 귀무 검정** (workflow c6)
- qlib 위치: `qlib/workflow/record_temp.py:575`(MultiPassPortAnaRecord). 단 qlib 코드는 첫날 초기 포지션만 셔플하는 init-민감도 체크라 **개념만** 취하고 코드는 버림.
- 왜: 그날 유니버스에서 점수↔종목 짝을 무작위로 섞어 오버나이트 손익 백분위 → "무작위 픽과 구별되는 엣지인가". close-bet v1의 "상위3=엣지0" 코호트를 더 빨리 걸렀을 것. DSR·cohort와 별개 제3 검정.
- 이식: talon 자체 엔진 위에서, IS 한정, 일별 유니버스/비용 존중.
- 판정: **adapt / next / M.**

**W22 — 시그널 오토코렐레이션·이름 오버랩** (eval c7)
- qlib 위치: `qlib/contrib/eva/alpha.py:116`(pred_autocorr), report/analysis_model(_pred_turnover).
- 왜/이식: fidelity.py의 top-K Jaccard를 하루-대-하루 랙으로. 같은 소형·비유동 종목 반복 픽 = 1000만원 시드 용량/집중 진단(10% ADV 캡·20%/5종목 한도와 충돌).
- 주의점: 해석용, go/no-go 아님. v2 점수가 있어야 유효.
- 판정: **adapt / later / S.**

**idea_only(원칙만 흡수, 배관은 버림)**: IC 안정성 히트맵/Q-Q(eval c2 — 헤드리스 JSON 관용과 충돌, 숫자만 W12에 접기), gross-vs-net 트윈(eval c4 — cohort가 pre-cost, 엔진이 net을 이미 분리; KrCostModel이 qlib보다 나음; "gross-only 헤드라인 금지" 규율만 유지), per-experiment 아티팩트 DAG(eval c5 — trials 레지스트리가 이미 DSR 다중검정 회계, RecordTemp 캐싱은 ADR 0010 과설계·stale 위험; code/data 해시 + OOS 봉인 강제만 추가=W2/W18), 시간축 분위 곡선(eval c6 — quantstats 티어시트가 이미 커버, qlib _group_return의 위치-슬라이스는 버그), risk_analysis 연율화(eval c8 — talon이 이미 product-mode MDD·DSR 헤드라인, qlib은 238/252 세 값이 불일치하니 복사 금지).

### 2.7 실험 관리

**W2 — 트라이얼 재현성 캡처** (workflow c3) — **now**
- qlib 위치: `qlib/workflow/recorder.py:362`(uncommitted git diff/status 자동 아티팩트), `:356`(argv/env).
- 왜: trials 테이블(state.py)이 params 문자열·sharpe·trades만 저장하고 git HEAD/diff/argv가 없다. IS 재튜닝 사이클(trial_cycles)을 반복하는 프로젝트라 승격 결과를 몇 달 뒤 되짚을 때 "어떤 코드"가 결정적.
- 이식: git HEAD + diff(텍스트, pickle 아님) + argv + canonical config dict를 기존 행에. **seed 캡처는 제외**(결정론 코어, RNG는 합성 크로스체크 생성기에만 존재 → 영구 사문).
- 판정: **adapt / now / S.**

**W18 — 전체 run 설정 직렬화 + 트라이얼 설정 해시** (workflow c1 + region c5)
- qlib 위치: `qlib/utils/mod.py:67`(init_instance_by_config, getattr 방식 — **eval 아님**), `qlib/config.py:64`(C 싱글턴).
- 왜: 안전 레지스트리(STRATEGY_FACTORIES/REGISTRY)는 이미 있음. 추가분: gate/regime/cost/universe를 하나의 재현 dict로 직렬화, EngineConfig+KrCostModel 해시를 트라이얼 행에 기록 — slippage_pct나 세율이 바뀌어도 DSR 트라이얼 카운트가 비교불가 run을 섞지 않게.
- 주의점: qlib의 dotted importlib 리졸버는 이식 금지(사실상 임의 코드 로딩), talon.* 화이트리스트만. 백테스트 경로에서 도달 가능한 레지스트리에 LLM 콜러블 절대 금지.
- 판정: **adapt / next / S.**

**reject**: 설정 상속 + env 템플릿(workflow c2 — Jinja 날짜 렌더가 ADR 0011 봉인 OOS 경계를 풀 구멍; talon 설정은 flat pydantic이라 deep-merge 불필요), Optuna SQLite(workflow c9 — 적응형 TPE가 DSR 정직 회계와 충돌; 소형 exhaustive 그리드엔 과함; ADR 0010 미승인 의존성). **idea_only**: 아티팩트 DAG·K-seed 분포·subprocess 격리(전부 결정론 코어/개인 규모에 조기 최적화).

### 2.8 재학습·드리프트

**핵심: 현재 ACTIVE_STRATEGIES 비고, 적합 모델 0개, 데이터 큐는 원시 수집(공매도/VI/신용).** 이 하위시스템 후보는 살아남을 전략이나 배포된 팩터를 전제하는데 아직 없다 → 대부분 next/later.

**W14 — 팩터 신뢰 IC 감쇠·부호반전 모니터** (online c4)
- qlib 위치: `qlib/contrib/meta/data_selection/dataset.py:107`(_calc_perf, 일별 spearman), `utils.py:40`(<50종목 스킵).
- 왜: 팩터별 롤링 RankIC를 유지, 추세 IC < k·IS IC면 축소, N일 부호반전이면 보류 — **축소/보류만** 하는 결정론 하드 게이트(ADR 0006·0002 부합). DDG-DA의 값비싼 ML 없이 실용 가치 ~80%.
- 이식: SURVIVAL_MIN_N을 최소 breadth로 재사용, 상하한 잠김일(분산 압축)은 제외, IS-frozen IC baseline.
- 주의점: 배포·적합된 팩터가 있어야 물릴 수 있어 페이퍼(Phase 3) 대상. IS 연구 진단으로는 cohort와 일부 겹침.
- 판정: **adapt / next / M.**

**W15 — 워크포워드 윈도 생성기 + OOS 스티칭(IS 한정)** (online c1/c5)
- qlib 위치: `qlib/workflow/task/gen.py:126`(trunc_segments), `qlib/contrib/rolling/base.py:218`(RollingEnsemble 스티칭).
- 왜: evaluate.py는 단일 고정 IS/OOS 분할뿐. 캘린더 인덱스 시프트 + trunc_days=horizon+1(close-bet horizon=T+1이라 ≥2 세션 절단) + 구간별 OOS 예측을 하나로 이어붙이는 정직한 워크포워드 곡선.
- 주의점: 순수 Python으로 KrxCalendar 위에 재구현(qlib 클래스·CN 9.5% 가정 금지). **봉인 2024-01-01 경계를 절대 넘지 말 것**(ADR 0011 one-shot) — IS 내부 재적합/강건성 윈도로만.
- 판정: **adapt / next / M.**

**reject/idea_only**: incremental daily-append(online c2 — reconcile.apply_official이 이미 정본 덮어쓰기, 점수 패널 아티팩트 자체가 없음, ADR 0010 과설계), 재학습 케이던스(online c3 — per-day 정규화는 CSRank로 이미, 적합 가중치는 ADR 0005 defer 트랙), 슬라이스 병렬화(online c6 — 미관찰 문제 + MacBook RAM 위험), 누수 마이크로패턴(online c7 — lookahead.py/verify_intraday로 이미 강제). **DDG-DA 메타 재가중(online c8)은 idea_only** — 실측 이득이 작고 비일관(2개 설정 중 1개만 인상적), PyTorch+45GB RAM, rank-deficient에서 실패, CN CSI300/20일 지평이라 KRW close-bet과 무관. 결정론 코어 방침과 충돌.

### 2.9 모델 (후일)

**핵심: ADR 0005가 ML 스코어링 트랙 전체를 Phase 5+로 게이트("규칙 전략이 페이퍼 생존 후").** 규칙 전략이 아직 Gate-1을 못 넘었으므로 모든 ML-후보는 선행조건 미충족.

**W21 — "테이블형=GBDT" 프레임** (models-bench c1) — **adapt / later / S**
- qlib 위치: `examples/benchmarks/README.md:29·75`.
- 왜: Alpha158(엔지니어드 테이블형)에서 DoubleEnsemble(연 0.1158) > LightGBM(0.0901) > 모든 신경망(최고 TRA 0.0718). 신경망은 원시 Alpha360에서만 앞서고 그마저 절대수익 더 낮음. talon은 분봉·그래프가 없어 신경망 동물원은 이중으로 무관.
- 이식: 새 문서 말고 ADR 0005/SYNTHESIS A18(이미 "LightGBM 정도로 충분")에 DoubleEnsemble-단일-업그레이드 + no-sequence-data 논거를 한 줄 병합, Phase 5+ 스탬프.
- 주의점: 숫자는 CN A주 2017-2020, 방향성만.
- 판정: **adapt / later / S.**

**idea_only (전부 Phase 5+ 노트)**: DoubleEnsemble(models c3 — 최고가치 테이블 업그레이드지만 GBDT baseline·엔지니어드 팩터 둘 다 아직 없음; decay=None 크래시·positional RangeIndex 정렬 함정 기록), LGBM 관례/HP prior(c4 — 무거운 L1/L2 정규화 insight만, KRX 유니버스는 더 무거워야), NNLS null 모델(c5 — 비음 계수=해석가능 롱온리 가중, 그러나 Gate-1엔 이미 KOSPI buy-and-hold null이 더 어려운 바), 앙상블 리듀서(c6 — 블렌드할 모델 0개; RollingEnsemble keep-last는 ADR 0011 단일 봉인과 상충).

---

## 부록 A: Alpha158/360 팩터 패밀리 (수식 요지·close-bet 유망도·KR 주의)

Alpha158 = KBAR 9 + 원시가격 4(OPEN0/HIGH0/LOW0/VWAP0) + 롤링 145(29 패밀리 × 윈도 5/10/20/30/60). Alpha360 = 6필드 × 60일 원시. 출처: `qlib/contrib/data/loader.py`. **모든 팩터는 T-1 기준 또는 15:10 스냅샷-앵커여야 함**(당일 종가·고저·전체거래량은 15:10에 없음, ADR 0013).

| 패밀리 | 수식 요지 | close-bet 유망도 | KR 주의 |
|---|---|---|---|
| **KBAR**(KMID/KLEN/KUP/KLOW/KSFT ±2) | 캔들 몸통·꼬리·종가위치 비율 9종 | 중 (스냅샷 변형=마감 직전 강한종가/짧은윗꼬리) | 상하한 잠김일 O=H=L=C로 퇴화. **v1이 이미 유사 상단꼬리 조건으로 실패**. 정직 백테스트는 ~3개월 실 15:10 패널로만 |
| **ROC / MA / STD**(모멘텀·이평·변동성) | Ref기반 수익, Mean, Std ÷ 종가 | 중 (단기 ROC·저 STD가 후보) | 이미 Delta/Mean/Std로 표현 가능. 원 ROC/STD 극단=+21% 스파이커(v1 실패 코호트) → winsorize 필수 |
| **BETA / RSQR / RESI**(추세 회귀) | 시간축 회귀 기울기·R²·잔차 | **상** (Rsquare=추세 매끈함, 스파이크 판별) | zero-variance NaN 가드 필수(상대오차). 정지일 span 왜곡. **v1이 못 가진 "raw 수익 너머" 필터** |
| **MAX / MIN**(롤링 극값) | 최근 N일 고/저 ÷ 종가 | 하 (오염) | 정확히 ±30% 스파이크가 사는 곳 — 유계 RANK/RSV 선호 |
| **QTLU / QTLD / RANK / RSV**(범위-포지션) | 0.8/0.2 분위, 롤링 백분위, %K=(종가−minLow)/(maxHigh−minLow) | **최상** (유계[0,1]·상하한 강건, "저점근처 vs 신고가돌파"=v1과 다른 가설) | RSV 분모 +1e-12 잠김일 가드. 대형주 오버나이트 엣지와 결합 |
| **IMAX / IMIN / IMXD**(Aroon 타이밍) | N일 고/저까지 경과일, 차이 | 하 | 단봉 노이즈, 상한 1봉이 "고점경과일"을 0으로 리셋. 다중검정 후 가지치기 예상 |
| **CORR / CORD**(가격-거래량 상관) | Corr(종가, log거래량, N) / Corr(수익, log거래량변화) | 중 (investor_flows 확정 연결 다리) | 미검증 가설. log(vol) 정지일 −inf. 패널에 flows 조인 선행 필요 → **idea_only/later** |
| **CNTP/CNTN/CNTD·SUMP/SUMN/SUMD**(방향·RSI) | 상승일 비율, 이득합÷|변화|합 | 중 (유계·평균회귀 방향 검정) | 새 연산자 불필요(Mean/Greater/Abs/Sum). 모멘텀 계열이라 range-position에 **종속** |
| **VMA/VSTD/WVMA·VSUMP/VSUMN/VSUMD**(거래량 동학) | 거래량 이평/표준편차, 거래량가중 |수익|변동, 거래량변화 RSI | 중 (WVMA=비유동 프록시) | 모든 /(vol+1e-12) 정지일 ~1e12. 당일 거래량 15:10 미관측 + **volume_1510 백필 불가**(2026-07-15부터) → **idea_only/later** |
| **Alpha360 원시**(6필드×60일 ÷ 최신종가) | 최소 엔지니어링 시퀀스 텐서 | 하 (ML 전용) | CLOSE0=1로 당일 종가 정규화 = 15:10 룩어헤드. T-1 정규화 필요. **Phase 5+ ML 입력 포맷으로만 보류** |

라벨 관례 주의: qlib 40개 설정 중 38개가 `Ref($close,-2)/Ref($close,-1)-1`(T+1 종가 진입, T+2 청산, 하루 skip). talon close-bet은 **당일 15:20-30 동시호가 진입**이라 skip 0 — 라벨은 재유도(복사 금지, ADR 0013이 이미 봉인).

---

## 3. 기각 목록 (reject/never·idea_only 핵심, 한두 줄)

**never로 명확히 배제(전량 이식 대상 아님):**
- **강화학습(RL) 주문집행 전체** (`qlib/rl/*`, 13파일 torch/tianshou) — talon은 동시호가 1회 체결, 장중 MDP 없음. 결정론 코어 방침(ADR 0001/0002)과 충돌.
- **고빈도 세션 연산자** (DayCumsum/DayLast/240분/봉 가정, `contrib/ops/high_freq.py`) — talon은 분봉 없이 15:10/15:35 스냅샷만. CN 09:30-11:30/13:00-15:00 점심시간 하드코딩이 KRX(연속 09:00-15:20+동시호가)에 오작동.
- **NestedExecutor 다층 집행·TWAP/SBB 분할** — 부모 주문을 분봉에 쪼개는 것, talon은 쪼갤 수 없음.
- **.bin float32 컬럼 스토리지** — KRW 거래대금/거래량이 float32 정수정확 한계(16.7M) 초과로 조용히 정밀도 손실. SQLite int64/float64가 더 안전(ADR 0010이 이미 기각).
- **eval(parse_field) 문자열 실행** (`data.py:397`) — ADR 0005 직접 위반. talon parser.py가 이미 ast 화이트리스트로 대체.
- **MongoDB TaskManager / MLflow 서버 / Redis 캐시 / redis-locked HDF5** — 클러스터·다중워커 인프라. 1 MacBook·SQLite·launchd엔 순수 운영 부채.
- **cvxpy/ECOS EnhancedIndexing 옵티마이저·POET/structured 공분산·PortfolioOptimizer** — 벤치마크 추적 수백 종목용. N=3 롱온리 절대수익엔 등가중이 추정오차 후 우세. ADR 0010과 충돌.
- **Optuna 적응형 튜닝** — TPE가 DSR 다중검정 회계를 부풀림/흐림. 소형 exhaustive 그리드엔 과함.
- **config env-템플릿** — Jinja 날짜 렌더가 봉인 OOS 경계를 풀 구멍(ADR 0011).
- **T+2 현금 지연 메커니즘** — KRX 매도대금 당일 재사용 허용이라 없는 제약 모델링 = 실전 괴리.
- **균등-현금 사이징** — ADR 0006 R-사이징과 충돌, v1 스파이커 실패 재현.
- **Ref sign forward-label 정제** (expr-ops c4의 "라벨엔 미래 Ref 허용" 부분) — ADR 0013이 닫은 룩어헤드 표면을 다시 열므로 **적극 회피**. talon은 자체 엔진 체결로 손익 라벨 계산.
- **CatBoost/XGBoost·PyTorch 동물원 25종·finetune·DDG-DA 메타모델·InfPosition·profit_attribution·TResample·Mask·N-오버로딩·리전 프리셋(REG_CN/US/TW)·set/reset 레이어링·EPS_T 반개구간 넛지·future-flag 이중 캘린더 원시함수** — 중복이거나 KR 미세구조에 부정확하거나 개인 규모에 불필요.

**idea_only(원칙만 기록, 코드 미이식)**: 위 2장 각 절의 idea_only 항목 — 대부분 talon이 이미 (더 엄격하게) 구현했거나, 살아남을 전략/배포 팩터/펀더멘털 값 수집기 같은 선행조건이 아직 없다.

---

## 4. qlib를 통째로 쓰지 않는 이유 (own-engine ADR + eval + 시장 구조)

세 축이 서로를 강화하며 "프레임워크 종속"을 배제한다.

1. **자체 엔진 ADR(0007).** talon은 백테스트·페이퍼·라이브가 한 체결 코드를 공유하도록 자체 경량 이벤트 드리븐 엔진을 만든다. qlib 엔진을 쓰려면 SQLite를 .bin으로 재덤프하거나 Exchange를 서브클래싱해 quote_df를 주입해야 하고, 그러면 NumpyQuote/IndexData/account/report/decision과 MongoDB/Redis/MLflow 가정까지 딸려 온다. 실제 체결 산술은 수백 줄에 불과하다 — **패턴을 이식하고 의존성은 지지 않는다.**

2. **eval() 금지(ADR 0005).** qlib 표현식 엔진은 문자열을 `eval(parse_field(field))`로 등록된 op 클래스 위에서 실행한다(`data.py:397`, `utils/__init__.py:277`). 연산자 **시맨틱**은 흡수 가능하나 **파싱 방식은 금지**다. talon parser.py는 이미 `ast.parse(mode='eval')` + 화이트리스트 `_convert`(속성/서브스크립트/dunder/keyword 거부, 길이·노드·상수 가드)로 동일 문법을 eval 없이 실현했다.

3. **시장 구조 차이.** qlib 디폴트는 전부 CN A주에 박혀 있다: limit_threshold 0.095(±9.5%), trade_unit 100(로트), T+1 매도 제한, 점심 휴장 240분 그리드, 매도 인지세를 close_cost에 접음. KRX는 ±30% 틱-그리드 상하한(ex-rights 기준가 조정), 1주 단위, **당일 왕복 허용(T+1 매도 제한 없음)**, 15:20-30 종가 동시호가(=일봉 종가 청산가), VI 장중 단일가, 연도별 매도세 스케줄. 이 값들을 그대로 빌리면 KRW 백테스트가 조용히 오작동한다. 게다가 qlib에는 **VI·종가 동시호가 체결 불확실성** 개념이 아예 없고, talon의 15:10/15:35-만-있는 스냅샷으로도 표현 불가하다 — talon 자체 미세구조 모델이 정답.

요컨대 흡수 대상은 **시맨틱(팩터 규율·워밍업 창·캘린더 정렬·QC 체크·평가지표)** 이지 **엔진·스토리지·파서**가 아니다. 그리고 그 시맨틱의 상당수는 talon이 이미 KR-정확 형태로 갖고 있다.

---

## 5. 다음 수순 제안 (착수는 사용자 몫)

**착수 결정은 전적으로 사용자에게 있다.** 아래는 현재 데이터 우선 단계(다음 데이터 후보: 공매도·VI·신용융자)와 충돌하지 않는 순서 제안일 뿐이다. 어느 것도 "지금 하라"가 아니다.

**A. 데이터 우선 단계와 병행 가능(지금 착수해도 단계 위배 아님):**
1. **W1 `talon health` 통합 QC 명령**(S) — 새 데이터셋(공매도/VI/신용)이 Gate-1에 들어오기 전 커버리지 공백·팩터 드리프트를 잡는 단일 패스. 데이터 우선 단계에 정확히 부합하는 유일한 now-후보. halt 마커(W17의 살릴 조각)를 함께 정의하면 NaN-vs-halt 구분까지.
2. **W2 트라이얼 재현성 캡처**(S) — git HEAD/diff/argv를 trials 행에. IS 재튜닝 사이클 전에 넣으면 이후 모든 트라이얼이 추적 가능. seed는 제외.

**B. 전략 v2가 열릴 때(현재 ACTIVE_STRATEGIES 비어 있어 아직 아님) — 하나의 응집 블록:**
3. **W3 v2 연산자**(Corr/Cov·Slope/Rsquare/Resi·WMA·Quantile·TSRank·Skew/Kurt) + zero-variance 가드 → 그 위에 **W4 range-position/RSV/트렌드품질 팩터 가설**. 이 둘이 새 작업 중 최고가치이며 함께 움직인다.
4. **W5 CSRobustZScore + W6 inf/null 정책** — 위 팩터들의 정규화·방어.
5. **W12 IC/RankIC/ICIR + W13 순열 귀무 + W16 중요도 트리아지** — v2 팩터를 트라이얼 예산에 태우기 전 평가 하네스. DSR/cohort와 직교하는 랭크-점수·귀무 검정을 우선 세우면 다중검정 낭비를 줄인다.
6. **W15 워크포워드 윈도+스티칭(IS 한정)** — Gate-1 하네스 확장. **봉인 2024-01-01 절대 불가침.**

**C. 엔진 하드닝(백테스트를 실제 돌릴 때):**
7. **W7 정밀 상하한 플래그 배선 + W8 동시호가 거래량 캡 + W9 손익 정합성 체크 + W10 결정가/체결가 이원화·슬리피지 캘리브레이션.** 지금은 물릴 전략이 없어 next이지만, 백테스트를 다시 돌리는 첫 순간에 체결 정밀도를 올린다. **W18 설정 해시**를 이때 함께.

**D. 트랙이 실제로 열릴 때만:**
8. **W14 팩터 신뢰 모니터**(페이퍼 Phase 3, 배포 팩터 필요), **W17 캘린더 리인덱스**(캘린더-길이 민감 팩터 실전 투입 시), **W20 DART PIT**(펀더멘털 값 수집기·스윙 트랙 착수 시 — 백필 가능하니 서두를 이유 없음), **W21 GBDT 프레임 ADR 병합**(Phase 5+ ML 트랙), **W22 용량 진단**(v2 엣지 확인 후).

**단일 최고 우선 착수 후보는 W1(데이터 QC)** — 데이터 우선 단계 부합, 저비용, 새 데이터 유입 전 방어. 나머지 전부는 전략 v2/엔진/페이퍼 트랙이 열린 뒤의 이야기다.