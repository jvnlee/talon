# FinMem-LLM-StockTrading 정밀 분석 (talon 관점)

- **레포**: https://github.com/pipiku915/FinMem-LLM-StockTrading (논문 arXiv 2311.13743, ICLR Workshop / AAAI Spring Symposium)
- **성격**: 학술 논문 재현 코드(연구 아티팩트). 최종 커밋 2024-08-17. 크기 56MB(대부분 데모 mp4).
- **한줄 요약**: 단일 종목 · 단일 LLM 에이전트가 "계층형 메모리 + 페르소나"로 매일 매수/매도/보유를 결정하는 구조. 백테스팅 엔진이나 라이브 어댑터는 사실상 없음.

---

## 1. 무엇을 하는 물건인가

FinMem은 **한 종목(TSLA, AMZN 등)** 에 대해 **하루 1회** buy/sell/hold를 내는 에이전트다. 핵심은 3개 모듈:
- **Profiling**: `character_string`(섹터 전문성·종목 배경을 담은 페르소나 텍스트).
- **Memory**: 4계층(short/mid/long/reflection) 벡터 메모리. 이 레포의 진짜 기여.
- **Decision-making**: 메모리를 조회해 LLM에게 결정을 시킴.

포지션 모델은 극도로 단순하다. 액션은 방향 `{-1, 0, +1}` × 수량 1주 고정. 손익은 `cumsum(diff(price) * holding_shares)`. 즉 **"1주 방향성 베팅의 정확도"** 를 측정할 뿐, 실제 포트폴리오·사이징·리스크가 없다.

## 2. 코드 구조 (`puppy/` 패키지)

- `agent.py` — `LLMAgent`: 스텝 루프 오케스트레이션(파일링/뉴스 적재 → 메모리 조회 → reflection → 포트폴리오 갱신 → 접근카운터 → 메모리 step).
- `memorydb.py` — `MemoryDB`(계층 1개 = FAISS `IndexFlatIP` + `SortedList` 점수 레코드)와 `BrainDB`(4계층 묶음). **가장 정교한 부분**.
- `reflection.py` — 프롬프트 조립 + **Guardrails-AI + Pydantic** 으로 JSON 스키마 강제 → LLM 호출.
- `prompts.py` — train/test 프롬프트 하드코딩.
- `chat.py` — `ChatOpenAICompatible`: httpx로 OpenAI/Gemini/TGI 엔드포인트에 raw POST.
- `environment.py` — `MarketEnvironment`: 날짜→{price, filing_k, filing_q, news} 피클을 순서대로 pop하는 **리플레이어**.
- `portfolio.py` — 보유주수, 피드백(룩백 손익 부호), 모멘텀(3일 누적수익 부호) 계산.
- `embedding.py` — **OpenAI `text-embedding-ada-002` 고정**(TGI/Gemini를 써도 임베딩만은 OpenAI 필수).
- `memory_functions/` — 지수감쇠, 중요도 초기화, 복합점수, 리센시.

## 3. 계층형 메모리 (핵심 차별점)

각 메모리 레코드: `{text, id, important_score, recency_score, delta, compound_score, access_counter, date}`.
- **적재 라우팅**: 뉴스→short, 10-Q→mid, 10-K→long, reflection 요약→reflection.
- **중요도 초기화**: LLM 판정이 아니라 **이산 분포에서 랜덤 샘플링**(short: 50/70/90를 p=0.5/0.45/0.05). 이건 약점.
- **리센시 감쇠**: `exp(-delta/recency_factor)`. 계층별 factor가 다름(short=3, mid=90, long=365일) → 단기기억은 빨리, 장기기억은 천천히 소멸(인간 인지 span 모사).
- **검색**: 쿼리 임베딩 = **에이전트의 `character_string`**(!). 유사도 top-k + 복합점수 top-k 2단 검색 후 `similarity + recency + importance/100`로 재랭킹. 즉 **검색이 페르소나에 편향**됨.
- **강화(access_counter)**: 매매 후 피드백(룩백 손익 부호)이 그 결정을 뒷받침한 메모리들의 중요도를 ±5씩 조정 → **수익을 낸 기억이 강화**.
- **점프(consolidation)**: 중요도 임계치를 넘으면 short→mid→long(또는 하향) 계층 이동. 인간 기억 공고화 모사.
- **정리(cleanup)**: 리센시/중요도 임계 미달 레코드 삭제 → 컨텍스트 상한 유지.

이 메모리 시스템은 **LLM을 전혀 호출하지 않고** FAISS+numpy로만 돈다. 임베딩만이 유일한 모델 의존.

## 4. LLM 오케스트레이션

- **단일 에이전트 · 단일 호출**. **멀티에이전트·토론·합의 없음.** 정교함은 "여러 에이전트"가 아니라 "계층형 메모리"에서 나온다.
- **Train 모드**: 에이전트에게 **미래 수익(다음날−오늘 가격)** 을 알려주고 "왜 이렇게 움직였는지 설명하라" → 그 설명 요약을 reflection 메모리에 적재. 즉 **미래 라벨로 메모리 뱅크를 warmup**. train의 액션은 실제 방향(수익>0→buy)으로 자동 설정.
- **Test 모드**: 미래 정보 없이 메모리 조회 + 모멘텀으로 buy/sell/hold 결정. Guardrails가 JSON 강제.
- **구조화 출력**: Pydantic 응답모델(`investment_decision` + `summary_reason` + 인용 메모리 id) + Guardrails `ValidChoices`. `num_reasks=1`이라 **하루 결정당 LLM 호출 1~2회**로 매우 저렴(호출 수 기준).
- **백엔드**: OpenAI(gpt-3.5/4), Gemini-pro(gcloud auth), HuggingFace TGI(자가호스팅 Llama2). 임베딩만 OpenAI 종량제 고정.

## 5. 백테스팅 설계 — talon에 가장 중요

**이것은 백테스터가 아니다.** 사전 생성한 일봉 피클을 날짜순으로 재생하는 **이벤트 리플레이**일 뿐이다.
- 체결 모델 없음. **수수료·슬리피지·호가·사이징 전무.** 항상 1주, 방향 ∈ {-1,0,+1}.
- **룩어헤드**: train 모드가 **미래 수익을 의도적으로 사용**해 메모리를 채운다(설계상 warmup). test 구간을 train보다 뒤로 엄격히 분리해야만 누수가 안 생김. (검색 쿼리가 고정 `character_string`이라 test 시 검색 자체는 미래정보 미사용.)
- **일봉·하루 1결정.** 인트라데이 불가 → **단타(데이트레이딩) 근본적으로 불가능**, 스윙도 아니고 사실상 "일간 방향 분류기".

## 6. 데이터 파이프라인 / 유지보수

- 별도 `data-pipeline/`: SEC(sec-api.io), Alpaca 뉴스, Refinitiv(비공개), LLM 요약, 감성(VADER/FinBERT), 가격(yfinance) → `env_data.pkl` 생성. **뉴스는 사전에 LLM으로 요약** 후 투입(추가 오프라인 LLM 비용).
- **유지보수 사실상 종료**(2024-08 마지막 커밋). 의존성이 낡음: `guardrails-ai 0.3.2`(현재 API와 대폭 상이), `langchain-community 0.0.15`, python 3.10 고정. 지금 그대로는 실행 난망. 연구 코드 품질(피클 상태 저장, 중복, `.env` 커밋, dead code, 버그).

---

## 7. talon 적용성 평가

### 그대로 채택(adopt): 없음
단일 종목 · 종량 API · 일봉 리플레이 구조는 talon 요구(한/미 다종목, 단타+스윙, 구독제 LLM, 실백테스트)와 정면 충돌.

### 패턴만 빌린다(borrow)
1. **계층형 메모리(감쇠+중요도+리센시+피드백 강화)**. 시황·종목 지식을 시간축으로 누적하는 리서치 에이전트에 유용. **핵심 장점: 조회·스코어링이 전부 로컬(FAISS+numpy)이라 LLM 호출 0회** → 구독제 제약과 궁합 매우 좋음.
2. **Reflection 루프**: 결과가 확정된 뒤 "왜 그렇게 됐나"를 LLM에게 설명시켜 교훈을 저장. Claude Code 구독으로 1콜/회 처리 가능. talon의 "매매 근거 제시" 역할과 직결.
3. **구조화 출력 강제**: 개념은 채택하되 죽은 guardrails 0.3.2 대신 **Claude tool-use / structured output**으로 구현.
4. **메모리 점프/정리**로 컨텍스트 상한을 능동 관리 → 구독제 context 한계 대응에 유용.
5. **페르소나 기반 검색 쿼리**: 관심 섹터로 메모리를 편향. talon은 사용자 관심/전략 프로파일을 쿼리로 재활용 가능(단, 편향 리스크 인지).

### 참고만(reference)
- train/test 메모리 warmup 개념 — 단, **구조적 룩어헤드**임을 명심하고 talon 백테스트 검증에 절대 누수시키지 말 것.
- 감성 파이프라인(VADER/FinBERT)은 **영어 전용** → 한국 뉴스엔 무용. 개념만 참고.

### 피한다(avoid)
1. **"백테스팅"을 모델 삼지 말 것.** 체결·비용·슬리피지·사이징 전무. talon은 별도의 이벤트 기반 엔진(체결모델·수수료·슬리피지·룩어헤드 방지)이 필수.
2. **1주 방향-only 액션 · 리스크 관리 부재**(손절·사이징·노출한도 없음) → 실자금엔 부적합.
3. **일봉 하루 1결정** → talon 단타 요구와 근본 불일치.
4. **종량 API 의존**(OpenAI 챗+임베딩). talon "구독제 전용" 규칙 위반. 챗은 Claude Code/Agent SDK로, **임베딩은 로컬 모델(sentence-transformers 등)로 반드시 대체**. 그 순간 메모리 시스템은 종량 비용 0으로 구동.
5. **영어·미국 시장 전제**(SEC/Alpaca/yfinance, ARK·Two Sigma 언급 프롬프트, FinBERT). 한국(KRX/토스 OpenAPI) 대응 전무.

### 한국 시장 적용 가능성
Korea 특화 요소는 전혀 없다. 데이터 소스를 **토스증권 OpenAPI + 한국 뉴스 + 한국어 임베딩 + Claude(요약/감성)** 로 전면 교체해야 함. 다만 **메모리 아키텍처 자체는 언어 불문**이라 이식 가능. FinBERT/VADER 감성만은 폐기.

### 구독제 LLM 궁합 결론
현 상태는 종량 API(챗+임베딩)라 **직접 호환 불가**. 그러나 설계가 잘 분리돼 있어, (a) reflection/결정(1콜/일)·(b) 뉴스 요약(오프라인) 은 **저빈도**라 Claude 구독으로 라우팅 가능하고, (c) 임베딩만 로컬화하면 **메모리·검색·스코어링 전 과정이 무과금**. 즉 "빌릴 패턴"은 구독제와 오히려 잘 맞는다.

## 8. talon 설계에 가져갈 구체 요소
- 로컬 임베딩 + FAISS 기반 **4계층 메모리(감쇠·중요도·리센시)** 를 리서치 에이전트의 지식 저장소로 채택(LLM 무과금).
- 매매 사후 **reflection→메모리 저장** 루프로 근거/교훈 축적(Claude 구독 1콜).
- **결과 피드백으로 메모리 중요도 강화**(수익 기여 기억 부스팅) 아이디어 차용.
- 구조화 출력은 **Claude tool-use**로 재구현(guardrails 폐기).
- 백테스팅·체결·리스크는 **이 레포에서 배우지 말고** 별도 전용 엔진으로 설계.
