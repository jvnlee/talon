# FinGPT 정밀 분석 리포트 (talon 설계 관점)

- 레포: AI4Finance-Foundation/FinGPT
- 분석일: 2026-07-09
- 최종 커밋: 2026-06-01 (활발히 유지보수 중, 다만 상업적/무관 코드가 유입되는 중)
- 결론 한 줄: **트레이딩/백테스팅 프레임워크가 아니라 "금융 LLM 파인튜닝·NLP 연구 저장소"다.** 그대로 쓸 것은 없고, 프롬프트 스키마와 데이터 위생 규율 정도만 빌린다.

---

## 1. 저장소의 실체

FinGPT는 이름과 달리 자동매매 엔진이 아니다. 핵심 산출물은 전부 **LoRA 파인튜닝된 금융 언어모델과 그 학습 파이프라인**이다.

| 모듈 | 성격 | talon 관련성 |
|---|---|---|
| `FinGPT_Sentiment_Analysis_v1/v3` | 금융 감성분석 LoRA (Llama2/ChatGLM). FinBERT·GPT-4 능가 F1 | 낮음 (영어 전용) |
| `FinGPT_Forecaster` | **트레이딩 관련 유일 핵심.** 주간 방향 예측 LoRA | 중간 (패턴 참고) |
| `ag2_financial_analysis_pipeline.ipynb` | AutoGen(AG2) 3-에이전트 오케스트레이션 | 중간 (구조 참고) |
| `FinGPT_RAG` / `FinGPT_MultiAgentsRAG` | 감성분석 RAG, 환각저감 MAS 토론 (MMLU 평가) | 낮음 |
| `FinGPT_Others/FinGPT_Trading` | 구식 ChatGPT 트레이딩 실험 (v1/v2) | 낮음 (반면교사) |
| `finogrid/` | **무관.** B2B 스테이블코인 송금 플랫폼 | 없음 (무시) |

`finogrid`는 아예 다른 프로젝트(국경간 USDT/USDC 정산, KYT/AML MCP 서버, MiniMax LLM)가 같은 레포에 얹힌 것으로, 주식 트레이딩과 무관하다. 저장소가 잡화점화되고 있다는 신호.

---

## 2. FinGPT-Forecaster (핵심 분석 대상)

### 동작
`app.py`/`prompt.py`/`data.py`를 직접 읽은 결과, Forecaster는 다음을 입력받아 **다음 주 주가 방향**을 예측한다.

- 입력: 회사 프로필 + 과거 N주(1~4) 뉴스 헤드라인/요약 + 분기 기초재무 + (옵션) 크로스소스 감성
- 출력: 고정 스키마 텍스트
  ```
  [Positive Developments]: 1. ...
  [Potential Concerns]: 1. ...
  [Prediction & Analysis]
  Prediction: Up/Down by {0-1%,1-2%,...,5+%}
  Analysis: ...
  ```
- 모델: Llama2-7B-chat + LoRA, **로컬 fp16 추론** (약 16GB VRAM, offload 폴더 사용)

### 학습 방식 — GPT-4 증류(distillation)
`data.py`의 파이프라인이 핵심이다.
1. yfinance로 주간 수익률 → `bin_mapping`으로 방향 라벨(U/D + %구간) 생성
2. finnhub로 뉴스·기초재무 수집
3. **GPT-4에게 실제 다음 주 결과를 알려주고("assume your prediction is X") 그 결과를 정당화하는 분석을 쓰게 함** (`query_gpt4`)
4. GPT-4 응답을 학생 모델(Llama2) 학습 데이터로 변환(`gpt4_to_llama`) → LoRA 파인튜닝

즉 Forecaster는 "강한 API 모델로 교사 데이터를 만들고, 값싼 로컬 모델로 증류"하는 구조다. 이 철학 자체는 비용통제 측면에서 talon과 통하지만(§5), 실행 방식은 talon 제약과 정면충돌한다.

### 백테스팅 — **사실상 없음**
Forecaster에는 체결 모델·수수료·슬리피지·워크포워드가 전혀 없다. 평가는 "생성된 방향이 실제 방향과 맞는가"라는 텍스트/분류 정확도뿐이다. P&L 곡선도, 포지션 사이징도, 리스크 관리도 없다. 저장소 전체에서 실제 백테스트는 `chatgpt-trading-v2`의 **pyfolio 토이 백테스트** 하나뿐인데, 이것도 "트윗 감성점수 S가 0.3 이상이면 100주 매수, -0.3 이하면 매도"라는 벡터화 임계값 전략을 거래비용 0으로 AAPL 2014–2015에 돌린 수준이다. talon이 넘어서야 할 **하한선**이지 참고 설계가 아니다.

### 룩어헤드 방지 — 부분적으로 양호
`prompt.py`가 뉴스를 `n['date'][:8] <= end_date`로 필터링하고, `get_current_basics`가 `period <= curday`인 최신 재무만 취하는 등 **시점정합(point-in-time) 규율은 존재**한다. 다만 (a) 교사 데이터가 결과를 알고 쓴 사후 합리화라 학생이 "근거 없이 자신 있게" 말하도록 학습될 위험, (b) DOW30 고정 유니버스라 생존편향 미처리, (c) finnhub 재무의 발표지연·정정 미반영 등 한계가 있다.

### 데이터 소스
yfinance(가격), finnhub(뉴스·프로필·재무), Adanos(옵션, reddit/x/news/polymarket 크로스소스 감성). 전부 미국/글로벌 소스이며 **한국 종목 커버리지는 사실상 없다.**

---

## 3. LLM 오케스트레이션 (AG2 파이프라인)

`ag2_financial_analysis_pipeline.ipynb`가 유일한 "멀티 에이전트 트레이딩" 예시다. (참고: `MultiAgentsRAG/MultiAgents` 폴더는 이름과 달리 MMLU 추론 노트북일 뿐, 금융 다중에이전트가 아니다.)

- **역할 분담**: News_Researcher(가격·뉴스 수집) → Sentiment_Analyst(FinGPT 감성분류) → Investment_Advisor(투자 브리프 합성). 여기에 도구를 실행하는 Executor 프록시.
- **오케스트레이션**: AG2 `GroupChat` + `GroupChatManager`, `speaker_selection_method="auto"` — **매니저 LLM이 다음 발화자를 매 라운드 선택**. 최대 10라운드, "TERMINATE" 문자열로 종료.
- **도구**: yfinance 기반 `get_stock_info`/`get_financial_news` + HuggingFace 감성모델 `analyze_sentiment`. 함수를 데코레이터(`register_for_llm`/`register_for_execution`)로 등록.
- **메모리/반성**: 없음. GroupChat 메시지 히스토리가 전부이고, reflection·토론합의·장기메모리 구조는 부재. RAG 모듈의 "MAS 토론"은 별개(환각저감용, 트레이딩 아님).
- **비용 구조**: 라운드마다 매니저 호출 + 발화 에이전트 호출이 발생 → **auto speaker selection은 호출량이 폭증**한다. 예시는 HF Inference API(Qwen2.5-Coder-32B, 종량 과금 엔드포인트)를 쓴다.
- **백테스팅 처리**: 아예 없음. 라이브 뉴스로 브리프 1건을 생성할 뿐, 과거 시점 재현·주문·체결 개념이 없다.

---

## 4. 유지보수·품질

- AI4Finance 재단이 활발히 유지(2026-06까지 커밋, PyPI 배포, 다수 스타). 다만 **연구·데모 코드 품질**이다: 노트북 중심, 테스트 빈약, 하드코딩된 경로/키, 상업적 무관 코드(finogrid) 유입.
- 프로덕션 트레이딩 인프라로 볼 수 없다.

---

## 5. talon 적용성 평가

### 그대로 채택 — 없음
설치해서 돌리는 트레이딩 프레임워크가 아니다. 채택할 실행 코드는 없다.

### 패턴만 빌릴 것
1. **Forecaster 출력 스키마**: `[Positive Developments]/[Potential Concerns]/[Prediction]/[Analysis]` 구조는 talon의 "매매 근거 제시" 애널리스트 에이전트에 그대로 쓸 만한 깔끔한 템플릿. **파인튜닝 없이 Claude에 이 스키마를 프롬프트로 주면** Llama2-7B보다 월등한 결과.
2. **시점정합 필터링 규율**(`date <= as_of`): talon 백테스터가 룩어헤드를 막는 기본기. 이 엄격함은 반드시 계승.
3. **감성을 구조화 팩터로 환원**(`market_sentiment.py`): 소스별 점수·커버리지·**정렬도(alignment/divergence)**·언급량을 수치 팩터로 집계하는 패턴은 talon 퀀트 팩터층에 이식 가능. "여러 소스가 일치하는가"라는 신호 품질 피처가 특히 유용.
4. **에이전트 역할 3분할**(리서처/감성/자문): talon 에이전트 분해의 좋은 골격.

### 참고만 할 것
- **AG2 GroupChat 자동 라우팅**: 역할 분해 아이디어는 참고하되, LLM 기반 발화자 자동선택은 호출량이 커 구독제 rate limit과 상충. talon은 **결정론적(고정 파이프라인) 오케스트레이션**을 택하고 LLM 호출을 아껴야 한다.
- **v2 pyfolio 임계값 백테스트**: talon이 반드시 능가해야 할 나이브 기준선. 이벤트 기반 엔진·거래비용·슬리피지·워크포워드가 왜 필요한지 보여주는 반면교사.
- **FinGPT 감성모델**: 영어 전용(미국 뉴스/트윗 학습). 한국 종목엔 부적합.

### 피할 것 (이유 포함)
1. **로컬 Llama2-7B/13B 자가호스팅**: 16~26GB VRAM GPU 필요. talon은 macOS PC + 구독제 LLM 환경 → 자가호스팅 부적합. 대신 **Claude를 구독 인증으로 사용**하면 더 강하고 제약에도 부합.
2. **GPT-4 종량 데이터 생성 + 파인튜닝 루프**: talon의 "API 종량 과금 절대 불가" 원칙 위반이며 MLOps 부담 가중. 파인튜닝 경로 전체를 회피하고 Claude 프롬프팅에 의존.
3. **주간 방향(Up/Down by X%) 타깃**: talon은 단타/스윙인데 뉴스 기반 주간 방향은 너무 거칠고 느리며 마이크로구조·기술적 지표를 무시. 이 타깃 정의는 채택 금지.
4. **v1 "ChatGPT 최면" 탈옥 트레이딩**: 조악·취약하고 리스크 프레이밍 부재. Claude엔 불필요·부적절. 회피.
5. **finogrid 전체**: 결제 도메인, MiniMax/무거운 인프라 유입. 무시.

### 한국 시장 적용 가능성
- 데이터 파이프라인이 전부 미국/글로벌(yfinance·finnhub). finnhub는 한국 커버리지 취약, yfinance는 `005930.KS` 시세는 받아도 뉴스/재무는 빈약. **talon이 토스 OpenAPI를 쓰는 결정이 옳다** — FinGPT는 한국 데이터 배관을 전혀 제공하지 않는다.
- 감성모델 영어 전용 → 한국어는 별도(KR-FinBERT류 또는 Claude 직접 프롬프팅) 필요.
- 반면 **프롬프트 스키마·팩터 집계 패턴은 언어 독립적**이라 이식 가능.

### 구독제 LLM 제약과의 궁합
- Forecaster 추론은 로컬(무 API)이라 표면상 구독제 친화적이나, **약한 모델을 GPU에 자가호스팅**해야 하므로 talon 환경과 안 맞음 → 순 부적합.
- AG2 자동 오케스트레이션은 호출 집약적 → 구독 rate limit에 불리. talon은 결정론적 파이프라인으로 호출을 절약해야 함.
- "강한 모델로 데이터 생성 → 값싼 로컬 모델 추론"이라는 증류 철학은 비용통제 지향에서 talon과 통하지만, 교사(종량 API 금지)·학생(GPU 없음/약함) 양쪽이 막혀 실현 불가.

---

## 6. talon이 가져갈 것 (요약)
1. 애널리스트 에이전트용 **구조화 출력 스키마**(발전/우려/예측/분석)를 Claude 프롬프트로 채택.
2. 백테스터 **시점정합·룩어헤드 방지 규율** 계승.
3. 감성을 **소스별 정렬도 포함 수치 팩터**로 집계.
4. LLM 오케스트레이션은 **결정론적 고정 파이프라인**으로(호출량·구독 제약 이유), AG2식 auto-routing은 지양.
5. 파인튜닝·로컬 모델·GPT-4 데이터생성·주간방향 타깃은 **전면 회피**.
