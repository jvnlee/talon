# RD-Agent (microsoft/RD-Agent) 정밀 분석 — talon 설계 관점

- 대상 레포: https://github.com/microsoft/RD-Agent (Microsoft)
- 분석 커밋: main, 최종 커밋 2026-05-06 (활발히 유지보수, Python 파일 529개)
- 관련 논문: R&D-Agent-Quant (NeurIPS 2025, arXiv 2505.15155), 전체 프레임워크 Tech Report (2505.14738)
- 라이선스: MIT

## 1. 정체성 요약 (한 문장)

RD-Agent는 "매매 봇"이 아니라 **데이터 중심 R&D 자동화 프레임워크**다. 'R'(아이디어 제안)과 'D'(구현)를 LLM으로 자동화하며, 퀀트 시나리오 **RD-Agent(Q)** 는 팩터/모델을 자동으로 발굴·코딩·백테스트·반영하는 진화 루프다. **라이브 트레이딩, 브로커 연동, 실시간 시세 처리는 전혀 없다** (`grep`으로 broker/live-trade/order-execution 코드 0건). 오프라인 리서치 배치 도구다.

## 2. 핵심 모듈 구조

`rdagent/core`에 시나리오 무관 추상화가 있고, `scenarios/qlib`가 퀀트 구현, `app/qlib_rd_loop`가 실행 진입점이다.

- **Hypothesis** (`core/proposal.py`): 가설 텍스트 + reason + concise_observation/justification/knowledge. 저자들이 주석에 "Belief"라 부름.
- **Experiment** (`core/experiment.py`): sub_tasks(팩터/모델 태스크) + 구현 워크스페이스 + hypothesis + `based_experiments`(직전 SOTA). 결과 metric 저장.
- **Trace** (`core/proposal.py`): `(Experiment, Feedback)` 노드의 **DAG**. `dag_parent`로 계보를 추적하고 `get_sota_experiment()`가 조상들 중 SOTA를 역추적. 병렬 다중 트레이스와 체크포인트 재개 지원.
- **진화 루프 5단계** (`components/workflow/rd_loop.py`): `direct_exp_gen`(제안+태스크화) → `coding`(구현) → `running`(백테스트) → `feedback`(반성) → `record`(트레이스 기록).

이 propose→implement→evaluate→reflect 루프가 프레임워크의 심장이며, 시나리오는 각 단계 클래스를 config로 주입(`import_class`)해 갈아끼운다.

## 3. 데이터 파이프라인 & 백테스팅 엔진 설계

**핵심: 자체 백테스트 엔진이 없다. Qlib(MS 퀀트 플랫폼)에 통째로 위임하고 Docker로 실행한다.**

- **표현 방식**: 팩터 = Qlib expression 또는 pandas 코드(팩터당 파일), 모델 = PyTorch `nn.Module` 코드(GRU/LSTM/tabular). 팩터·모델 공동 최적화(co-optimization).
- **실행 경로** (`developer/factor_runner.py`): 팩터 코드 실행 → DataFrame 산출 → SOTA 팩터와 **IC 기반 중복 제거**(IC>0.99면 폐기) → 병합 → parquet 저장 → Qlib config YAML(Jinja 템플릿)로 Docker 백테스트 → mlflow 메트릭 회수.
- **체결/비용 모델** (`conf_baseline.yaml`): `TopkDropoutStrategy`(예측 시그널 상위 50 보유, 하위 5 교체), `deal_price: close`, 수수료 `open_cost 0.0005 / close_cost 0.0015 / min_cost 5`, 가격제한 `limit_threshold 0.095`. **벡터화/포트폴리오 레벨 백테스트이며 이벤트·틱 기반이 아니다.**
- **룩어헤드 방지**: label을 미래 수익률(`Ref($close,-2)/Ref($close,-1)-1`)로 정의, train/valid/test **시간 분할**, 정규화(`RobustZScoreNorm`) fit 구간을 train으로만 한정.
- **데이터 지역**: `market: csi300`, `region: cn` — **중국 A주 기본**. US는 Qlib 데이터로 가능하나 기본값 아님. **한국 시장/토스 지원 없음.**
- **리스크 관리**: 전략 레벨(topk-dropout, 가격제한) 외 별도 리스크 모듈 없음. 평가 메트릭에 MDD/Sharpe 포함.

## 4. LLM 오케스트레이션 (상세)

**에이전트 역할 분담** — 다중 에이전트 "토론/합의"가 아니라 **순차 파이프라인 + 진화**다:
1. `HypothesisGen`(리서치, R): 트레이스 히스토리를 보고 다음 가설 제안.
2. `Hypothesis2Experiment`: 가설을 구체 태스크로 변환.
3. `CoSTEER` Coder(구현, D): 진화적 코드 생성.
4. `Experiment2Feedback`(반성): 백테스트 결과를 SOTA와 비교해 평가.

**Action selection (factor vs model)** — `proposal/bandit.py`: 다음 실험이 팩터 개선인지 모델 개선인지를 **선형 Thompson Sampling 밴딧**으로 결정(기본값 `bandit`). 8차원 메트릭 벡터(IC, ICIR, RankIC, ARR, IR, -MDD, Sharpe)에 가중 보상. LLM/random 옵션도 있으나 **밴딧은 LLM 호출 없이 통계적으로** 방향을 고른다 — 구독 제약과 궁합이 좋은 설계.

**CoSTEER (LLM 호출량의 주범)** — `components/coder/CoSTEER`: 코드 생성을 최대 `max_loop=10` 반복하며 각 반복마다 (코드 생성 LLM 호출 + 실행/평가). **RAG 지식베이스**에 성공/실패 코딩 경험을 누적해 재사용. `fail_task_trial_limit=20`.

**메모리·반성 구조**:
- **Trace DAG** = 장기 메모리(가설·결과·SOTA 계보). 프롬프트에 히스토리 주입(`hypothesis_and_feedback`, `last_...`, `sota_...`로 분리).
- **Feedback** = reflection: 관찰(Observations)/가설평가/새 가설/결정(SOTA 교체 여부)을 **JSON 구조화 출력**으로 생성. `quant_proposal.py`는 factor 액션이면 factor 히스토리+SOTA 모델만, model 액션이면 그 반대만 선별 주입해 컨텍스트를 압축.
- **CoSTEER 지식베이스** = 코딩 경험 메모리.

**프롬프트 설계**: Jinja 템플릿(`utils/agent/tpl.py`) + YAML 프롬프트 파일(`prompts.yaml`), `json_mode` 강제, 백테스트 메트릭을 표로 렌더링해 주입.

**비용 구조 (talon 핵심 이슈)**: 백엔드는 **LiteLLM**(기본)/OpenAI/Azure. `.env.example`은 `OPENAI_API_KEY`·`OPENAI_API_BASE`를 요구하고, `litellm.py`는 `completion_cost`로 **토큰당 과금을 `ACC_COST`에 누적**한다. RAG용 임베딩 모델도 별도 API 필요. 논문 기준 RD-Agent(Q) 1회 완주 "**<$10**"(GPT-4.1/o3). 루프당 호출 = 가설 1~2 + CoSTEER 약 10×2 + 피드백 1~2, 여기에 `evolving_n=10` → **회당 수백 LLM 호출**.

**백테스팅을 어떻게 처리하나**: 회피하지 않는다. Qlib 실백테스트를 **실제로 실행**하고, 그 정량 메트릭(IC/ICIR/ARR/MDD/Sharpe)을 밴딧 보상과 LLM 반성의 입력으로 쓴다. **LLM은 백테스트를 "판단"할 뿐 실행하지 않는다** — 정량 평가와 정성 판단의 명확한 분리가 이 설계의 강점.

## 5. 유지보수 상태

매우 활발. 최근 커밋 2026-05, LiteLLM 백엔드·웹 UI·MLE-bench 1위·NeurIPS/ICML/ACL 게재. mypy·ruff·CI 완비. 신뢰할 만한 참조 대상.

## 6. talon 적용성 평가

### 그대로 채택할 것: 없음
근거 4가지가 모두 talon 제약과 정면 충돌한다:
1. **구독제 LLM 불가**: LiteLLM/토큰 과금 전제. Claude Max/Codex Pro 구독 인증 경로가 아예 없다. → talon 하드 제약 위반.
2. **라이브 트레이딩 부재**: 브로커/시세/주문 어댑터 0. 오프라인 리서치 배치 도구.
3. **한국 시장 미지원**: CSI300 중심, 토스 API와 무관.
4. **인프라 과중**: Qlib + Docker + 일봉 크로스섹셔널 팩터투자 전제 — talon의 단타/데이트레이딩과 매매 지평이 상충.

### 패턴만 빌릴 것 (가치 높음)
1. **R&D 진화 루프 추상화** (Hypothesis→Experiment→Backtest→Feedback→Trace): talon의 "전략 리서치 루프"에 이식. 단 오케스트레이션은 Claude Code(구독)로.
2. **SOTA 추적 Trace/DAG**: 전략·팩터 버전 계보 관리와 재개(체크포인트) 설계의 좋은 청사진.
3. **밴딧 액션 선택 (Thompson Sampling + 메트릭 벡터 보상)**: LLM 없이 "다음 실험 방향"을 통계적으로 결정 → **구독 rate limit 아래서 LLM 호출을 아끼는 핵심 패턴.** talon에 직접 이식 권장.
4. **팩터 IC 기반 중복 제거**: 팩터 라이브러리 비대화 방지.
5. **Factor-from-report** (`factor_experiment_loader/pdf_loader.py`): 리서치 리포트 PDF에서 팩터를 LLM으로 추출·분류. **talon의 뉴스/리포트 리서치 → 시그널화에 직결되는 가장 재사용성 높은 아이디어.**
6. **정량/정성 분리 + 구조화 피드백**: 백테스트 메트릭은 코드가 계산, LLM은 관찰/평가/결정(JSON)만. 반성 히스토리를 프롬프트에 선별 주입.

### 참고만 할 것
- **Qlib 백테스트 설정 = 자체 백테스터 설계 체크리스트**: 수수료/가격제한/룩어헤드 방지(시간분할·정규화 fit 구간 한정)는 그대로 참고. 단 talon은 **이벤트 기반 + 단타 체결 모델**이 필요하므로 Qlib의 일봉 벡터화 방식은 재사용 불가 → backtrader/vectorbt 또는 자체 구현 검토.
- CoSTEER의 "코드 생성/평가 분리 + 지식 누적" 개념은 참고하되, talon에선 Claude Code 자체가 그 역할을 대신하므로 별도 구현 불필요.

### 피할 것
- **토큰 과금 전제의 대량 LLM 호출 아키텍처**(CoSTEER 10회 × evolving_n 10회): 구독 rate limit에 즉시 부딪힘. talon은 LLM을 리서치/판단에만 쓰고 탐색·선택·백테스트는 결정론 코드로.
- **Qlib 통째 채택**: 중국 일봉 팩터투자 프레임을 한국·단타로 재작업하는 비용이 과다.
- **Docker 워크스페이스 격리 코드 실행**: 1인 초기 규모엔 과함.

## 7. 구독제 LLM 궁합 결론

RD-Agent 자체는 종량 API 전제라 talon에 **직접 이식 불가**다. 그러나 역설적으로, 이 프레임워크에서 **LLM 호출을 최소화하는 요소들**(밴딧 액션 선택, 결정론적 백테스트로 평가 위임, 컨텍스트 선별 주입, 캐시)은 오히려 talon의 구독 제약과 궁합이 매우 좋다. 이상적 방향은 RD-Agent의 hybrid(bandit+LLM)에서 **LLM 비중을 더 낮춘 버전**: "리서치·판단"만 Claude Code로, "탐색·선택·백테스트·리스크"는 결정론 코드로 분리한다.

**최종 판정: reference (핵심 패턴은 borrow).** 구조를 배우되 코드를 가져오지 않는다.
