# 정밀 분석: virattt/ai-financial-agent

- **레포**: https://github.com/virattt/ai-financial-agent
- **분석 대상 커밋**: `main` (last push 2025-08-19)
- **상태**: 2.0k stars / 405 forks / open issues 5 / Apache-2.0(Vercel 템플릿 상속) / 2024-12 생성
- **한 줄 요약**: Vercel "AI Chatbot" 템플릿을 미국 주식 리서치 챗봇으로 개조한 **단일 에이전트 PoC**. 백테스팅·전략·리스크·주문·한국시장 전부 부재.

## 1. 정체성 — 이것은 무엇이 "아닌가"

먼저 오해를 걷어내야 한다. 이 레포는 이름과 달리 **트레이딩 시스템도, 퀀트 엔진도 아니다.** 저자(virattt)의 유명한 `ai-hedge-fund`와 혼동하기 쉽지만 완전히 별개다. 전체 소스 ~14k 라인 중 대부분이 Next.js 15 + React UI 보일러플레이트(ProseMirror 에디터, 인증, Drizzle ORM, shadcn/ui)이고, 실제 "금융 에이전트" 로직은 **3개 파일 약 600라인**에 불과하다:

- `app/(chat)/api/chat/route.ts` — 오케스트레이션
- `lib/ai/tools/financial-tools.ts` — 7개 데이터 툴
- `lib/ai/prompts.ts` — 5줄짜리 시스템 프롬프트

`grep`으로 backtest / slippage / portfolio / broker / order / position-sizing / signal / factor를 전수 검색했으나 **실질 매칭 0건**(전부 CSS·UI 오탐). 즉 **백테스팅 엔진, 체결 모델, 수수료/슬리피지, 룩어헤드 방지, 리스크 관리, 라이브 트레이딩 어댑터가 모두 존재하지 않는다.** README도 "educational only, not for real trading"을 명시한다. talon이 원하는 검증 파이프라인 관점에서 배울 코드가 이 레포엔 없다.

## 2. LLM 오케스트레이션 (핵심 상세)

talon 지시대로 이 부분을 가장 깊게 봤다. 구조는 **2단계 파이프라인**이며, Vercel AI SDK v4(`ai@4.0.20`) 위에서 돈다.

**1단계 — 태스크 분해 (`generateObject`)**: 사용자 쿼리를 받아 `gpt-4.1-nano`로 Zod 스키마(`{task_name, class}[]`) 기반 구조화 출력을 뽑아 **1~3개 서브태스크**로 쪼갠다. 프롬프트는 "재무 추론 에이전트로서 쿼리를 tightly-scoped 서브태스크로 분해, 현재진행형 동사(Analyzing/Retrieving/Comparing) 3~7단어"로 지시. **이 분해의 실제 용도는 UX 투명성뿐이다** — 태스크 이름을 로딩 스트림(`query-loading`)에 흘려 사용자에게 "분석 중" 스텝을 보여준다. 진짜 계획 실행엔 쓰지 않는다.

**2단계 — 에이전틱 툴 루프 (`streamText`)**: 사용자가 고른 모델(기본 `gpt-4o`)에 7개 툴을 붙이고 `maxSteps: 10`으로 ReAct식 툴콜 루프를 돌린다. 특이점: 마지막 user 메시지 content를 **1단계에서 뽑은 태스크 리스트 텍스트로 통째 교체**해서 넣는다(원본 질문 문맥이 유실되는 취약한 해킹). 스트림이 잘리는 걸 막으려 `onFinish`에 `setTimeout(1000)`을 박아둔 것도 PoC 수준.

**부재한 것들 (talon 설계상 중요)**:
- **멀티에이전트 없음.** 역할 분담·토론·합의(debate/consensus) 메커니즘 전무. 단일 에이전트가 툴만 부른다.
- **반성(reflection) 루프 없음.** self-critique, 재시도 판단 없음.
- **장기 메모리 없음.** "메모리"는 Postgres `Message` 테이블에 쌓이는 대화 히스토리가 전부. 벡터스토어·요약메모리·매매일지 없음.
- **프롬프트가 극도로 빈약.** 시스템 프롬프트(`regularPrompt`)는 "친절한 재무 비서, 간결히, 마크다운/표/리스트 금지, ttm 기본, API 호출 최소화" 5줄. 금융 도메인 지식·가드레일·투자 원칙이 주입돼 있지 않다.

**LLM 호출량·비용 구조**: 쿼리당 최소 3콜 — (a) 채팅 최초 1회 제목생성(`gpt-4.1-mini`), (b) 태스크 분해(`gpt-4.1-nano`), (c) 메인 에이전트(`gpt-4o`, 최대 10 툴스텝). 전부 **OpenAI 종량 과금 API**. BYO-key 모델(사용자가 자기 OpenAI 키를 요청 body로 전달, localStorage/DB 보관). LangSmith 트레이싱 옵션 내장.

**백테스팅을 어떻게 처리하는가?** — **회피한다.** 시간 축을 되감아 과거 시점에 에이전트를 재현하는 개념 자체가 없다. 항상 "지금" 데이터를 조회하는 실시간 리서치 도구다.

## 3. 데이터 파이프라인

- **단일 소스**: Financial Datasets API(financialdatasets.ai), **미국 주식 전용**, 30년 100% 커버리지. 무료는 AAPL/GOOGL/MSFT/NVDA/TSLA 5종뿐.
- **7개 툴** (전부 얇은 `fetch` REST 래퍼 + Zod 스키마 + 풍부한 description):
  `getStockPrices`(스냅샷+과거가, second~year 인터벌), `getIncomeStatements`, `getBalanceSheets`, `getCashFlowStatements`, `getFinancialMetrics`(P/E 등 파생), `searchStocksByFilters`(약 90개 재무 필드 스크리너, gt/lt/eq 연산자), `getNews`.
- **캐싱·로컬DB·레이트리밋·재시도 전무.** 유일한 최적화는 `shouldExecuteToolCall`: 한 요청 내에서 `{toolName,params}` 해시를 Set에 넣어 중복 툴콜만 스킵하는 인메모리 dedup.

## 4. talon 적용성 평가

**총평: 참고(reference) 등급.** 그대로 채택할 코드는 없고, 몇 가지 **패턴만 빌릴** 가치가 있다. 이유는 두 개의 구조적 불일치다.

**(a) 구독제 LLM 제약과의 궁합 — 근본적 불일치.** 전 스택이 `@ai-sdk/openai` 종량 API에 묶여 있다. talon은 Claude Max/Codex Pro **구독 인증**만 허용하므로, `streamText`/`generateObject`/BYO-key 플럼빙을 전부 Claude Agent SDK(또는 Claude Code) 구독 세션으로 갈아끼워야 한다. 다행히 **오케스트레이션 "패턴"(구조화 분해 → 툴 루프)은 프레임워크 독립적**이라 Claude Agent SDK의 tool-use 루프로 1:1 이식 가능하다. 배관은 버리고 형태만 가져온다.

**(b) 한국 시장 — 커버리지 0.** Financial Datasets는 미국 전용이라 한국 종목 데이터가 전무. 모든 데이터 툴을 **토스증권 OpenAPI 기준으로 재작성**해야 한다. 단, "툴 = Zod 스키마 + 얇은 fetch 래퍼 + LLM 친화적 description" 패턴은 토스 API 래핑에 그대로 적용된다.

**(c) 단타/스윙 부적합.** 이 레포는 재무제표·스크리너 중심의 **펀더멘털 리서치**에 편향. `getStockPrices`가 분/초 인터벌을 지원하긴 하나, **기술적 지표·차트분석·시그널이 0개**라 데이트레이딩엔 뼈대만도 못 된다. talon은 지표/차트/시그널 레이어를 자체 구축해야 한다.

**(d) 백테스팅 — 배울 것 없음.** talon의 1순위 요구(광범위한 백테스팅)에 대해 이 레포는 참고자료가 되지 못한다. 별도 레포(예: 이벤트 기반 백테스터)를 봐야 한다.

### 빌릴 패턴 (borrow)
1. **툴 정의 규약**: Zod 스키마 + 서술적 description + 얇은 fetch 래퍼. 토스 OpenAPI 래핑의 기본형으로 채택.
2. **태스크 분해 = UX 투명성**: 분해 결과를 실행이 아닌 "진행상황 표시"에 쓰는 아이디어는 **텔레그램 봇**에 적합("분석 중: 삼성전자 수급 확인 →…" 진행 메시지). 단, 원본 메시지 교체 해킹은 버리고 태스크는 별도 컨텍스트로 첨부.
3. **구조화 출력(`generateObject`+Zod)**: 매매근거·시그널을 기계가독 JSON으로 강제하는 패턴 — talon의 "매매 근거 제시"에 유용.
4. **툴콜 dedup 캐시**: 레이트리밋 있는 토스 API·느린 LLM 반복호출 절감에 직접 유효(요청 범위 → 세션/시간 범위로 확장).
5. **모델 레지스트리 추상화(`models.ts`)**: 모델 교체 지점 일원화. Claude 모델 스왑에 재활용.
6. **스크리너 필드 어휘(약 90개)**: `stock-filters.ts`의 재무 팩터 목록은 talon 팩터 설계 시 체크리스트로 참고.

### 피할 것 (avoid)
- OpenAI/Vercel 종량 스택 통째(구독 제약 위반).
- user 메시지를 태스크 리스트로 교체(문맥 유실).
- 시세 로컬 캐시/DB 부재 → 토스 API 남용 위험. talon은 반드시 로컬 저장 계층 선행.
- `setTimeout(1000)` 스트림 해킹, `any` 타입 dataStream, 무재시도 — PoC 견고성. 프로덕션 금물.
- 이 레포를 "트레이딩/퀀트 참조 구현"으로 오인하는 것 자체.

## 5. 결론

talon 관점에서 이 레포의 가치는 **"툴콜 기반 금융 리서치 챗봇의 최소 골격 + 텔레그램 UX 힌트"**로 한정된다. 아키텍처는 깔끔하고 읽기 쉬워 초기 에이전트 스캐폴딩의 형태 참고엔 좋지만, 백테스팅·리스크·전략·한국시장·구독LLM 어느 것도 해결해주지 않는다. **채택 0, 패턴 차용 소수, 나머지는 반면교사.**
