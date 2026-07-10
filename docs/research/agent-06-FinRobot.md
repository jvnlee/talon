# FinRobot 정밀 분석 (talon 설계 관점)

- 레포: AI4Finance-Foundation/FinRobot (ICAIF 2024 논문 계열)
- 분석 커밋: `297a8d2` (README 갱신), 클론 37MB
- 결론 한 줄: **미국 주식 "리서치 리포트 생성기"이자 LLM 오케스트레이션 데모. 트레이딩/백테스팅 인프라는 얇고, 구독제 LLM과 한국 시장 양쪽 모두와 궁합이 나쁘다. 오케스트레이션 "패턴"만 빌린다.**

## 1. 레포 구성

두 개의 이질적 패키지가 한 레포에 섞여 있다.

- **`finrobot/`** (원조): AutoGen(`pyautogen>=0.2.19`) 기반 멀티에이전트 라이브러리. 데이터소스·분석함수·백테스트 래퍼.
- **`finrobot_equity/`** (신규): **OpenAI Agents SDK**(`from agents import Agent, Runner`) 기반 종목 리서치 리포트 생성기 + FastAPI 웹앱(DB/인증/관리자). 8개 섹션 에이전트가 PDF/HTML 리포트를 만든다.

두 패키지는 프레임워크·LLM 프로바이더·코드 스타일이 모두 다르다(equity 쪽엔 중국어 주석 잔존). 실질 유지보수는 "새 모듈을 계속 붙이는" 방식이고, 코어는 구형 AutoGen 0.2.x + `gpt-4-0125-preview`에 고정되어 있다. 의존성 핀이 서로 충돌(`pandas==2.0.3` vs `>=2.0`)한다. 연구 데모 성격이 강하다.

## 2. 데이터 파이프라인

- 소스: `FinnHubUtils`, `YFinanceUtils`, `FMPUtils`(FinancialModelingPrep), `SECUtils`(sec_api), `RedditUtils`, FinNLP. **전부 미국 시장 전용**이며 대부분 유료 API 키 필요.
- 구조: 각 소스는 정적 메서드 클래스이고 pandas DataFrame을 반환 → `stringify_output`으로 문자열화해 프롬프트에 삽입.
- **정규화된 도메인 데이터 모델이 없다.** 통합 스키마, PIT(point-in-time) 펀더멘털, 생존편향 처리, 캐싱 계층 전부 없음. LLM 응답 캐시(`Cache.disk`, `cache_seed=42`)만 존재하며 이는 시세 캐시가 아니다.
- **한국 시장 지원 0.** KRX/KOSPI/KOSDAQ 코드 없음. `tushare`(중국)는 requirements에만 있고 코어 미사용. talon은 토스 OpenAPI 기반으로 데이터 계층을 전면 교체해야 한다.

## 3. 전략 표현 · 백테스팅 엔진

백테스트는 **backtrader 얇은 래퍼**(`functional/quantitative.py::BackTraderUtils.back_test`) 하나가 전부다.

- **이벤트 기반**(backtrader `next()` 바 단위) — 벡터화가 아니므로 엔진 자체의 룩어헤드 위험은 낮다. 이 선택은 합리적.
- 애널라이저로 SharpeRatio/DrawDown/Returns/TradeAnalyzer 부착.
- 전략 표현: `SMA_CrossOver` 프리셋 또는 `"module:ClassName"` 문자열로 커스텀 Strategy/Sizer/Indicator 동적 임포트.

치명적 결함들(talon 경고):

- **수수료·슬리피지 전무.** `setcommission`도, 슬리피지 모델도 없다. 기본값 = 수수료 0·슬리피지 0. 한국 단타는 거래세+슬리피지가 손익을 결정하는데, 이 백테스트 결과는 신뢰 불가.
- **사이징/리스크 규칙 없음** (기본 FixedSize 옵션만).
- **인샘플 과적합 워크플로우**: `agent_trade_strategist` 튜토리얼은 에이전트에게 "2022–2024 데이터로 전략을 개발하고 **같은 기간**에 백테스트해서 Buy&Hold를 이겨라"고 지시하고 결과 차트를 보며 반복 최적화시킨다. train/test 분리, 워크포워드, OOS 검증이 전무하다. 전형적 데이터 스누핑.
- Sharpe는 backtrader 기본(바 단위, 무위험수익률 미조정)이라 값 자체가 순진하다.

즉 "퀀트 전략"은 구조화된 팩터/시그널 라이브러리가 아니라 **LLM이 그때그때 파이썬 코드를 써서 UserProxy가 실행**하는 임시 코드 생성이다(`experiments/multi_factor_agents.py`도 동일). 재현·검증 가능한 전략 표현 계층이 없다.

## 4. 리스크 관리 · 라이브 트레이딩

- **프로그램적 리스크 관리 없음.** investment_group의 "Risk Assessment Analysts"는 프롬프트 롤플레이일 뿐 포지션 한도·VaR·손절 강제·포트폴리오 제약이 코드로 없다.
- **라이브/페이퍼 트레이딩 어댑터 없음.** 브로커 연동·주문 라우팅·OMS 부재. 데이터는 전부 읽기 전용. 이것은 **트레이딩 시스템이 아니라 리서치/리포트 도구**다. talon의 토스 주문 연동에 재사용할 것이 없다.

## 5. LLM 오케스트레이션 (핵심)

talon이 실제로 볼 유일한 알맹이.

- **역할 정의**: `agent_library`가 `{name, profile(프롬프트), toolkits}` 딕셔너리로 에이전트를 선언적으로 등록. `title/responsibilities`를 `role_system_message`/`leader_system_message` 템플릿에 끼워 시스템 프롬프트 생성.
- **두 가지 토폴로지** (`agents/workflow.py`):
  1. **그룹챗**(`MultiAssistant`): 커스텀 `speaker_selection_func`로 발화자 선택.
  2. **리더-부하**(`MultiAssistantWithLeader`): 리더가 `"[AgentName] 지시"` 문자열을 뱉으면 정규식 트리거가 해당 에이전트의 nested chat으로 라우팅. 부하는 max 10턴/자동응답 3회로 실행하고 `reflection_with_llm`로 요약해 리더에 반환. **깔끔하고 이식성 좋은 패턴.**
- **"토론/합의"**: multi_factor의 `without_leader` 설정에서 3명의 Fundamental Analyst에게 "서로 조언 구하고 합의하라"고 **프롬프트로만** 지시. 투표·중재·집계 프로토콜이 있는 구조적 debate가 아니다. 형식적 합의.
- **메모리/반성(reflection)**:
  - 세션 내 AutoGen 대화 히스토리 + `reflection_with_llm` 요약 압축.
  - `SingleAssistantShadow`: 실행 전 "그림자" 에이전트가 지시를 2턴 검토(경량 self-review).
  - **영구 메모리·과거 매매 반성 DB·벡터 메모리 없음.** 디스크 캐시는 동일 호출 dedup 용도.
- **LLM 호출량/비용**: 매우 높음. 리더 루프·그룹챗이 10~30턴, 매 턴 GPT-4 호출. 코드작성 에이전트는 작성→실행→수정 반복. equity는 8개 에이전트가 웹서치+장문 출력. **종량 API 기준 비싸다.** `cache_seed`로 반복 실행 시만 절감.
- **백테스팅 처리 방식**: 체계적 검증을 사실상 **회피**한다. LLM이 전략을 쓰고 같은 창에서 한두 번 돌려보고 눈대중으로 반복. 교차검증 없음.

## 6. LLM 프로바이더 종속성 (구독제 제약 대비)

- 코어는 **OpenAI GPT-4에 하드와이어**(`OAI_CONFIG_LIST` model 필터 = gpt-4-0125-preview). AutoGen은 `base_url` 오버라이드를 지원해 Ollama/로컬 OpenAI 호환 엔드포인트는 가능하며 실제 ollama 튜토리얼도 있다.
- 그러나 **Claude/Anthropic 네이티브 지원 없음**, 구독 인증(Claude Max) 경로 없음. equity 모듈은 OpenAI Agents SDK+`openai>=1.0.0`으로 더 강하게 종속.
- **talon 결론**: FinRobot의 실행 스택은 "OpenAI 호환 종량 엔드포인트" 전제. Claude Max 구독(Agent SDK) 인증 모델과 맞지 않는다. AutoGen을 그대로 상속하지 말고 **Claude Agent SDK 위에 오케스트레이션 패턴만 재구현**해야 한다.

## 7. talon 적용성 판정

**그대로 채택(adopt): 없음.**

**패턴만 빌릴(borrow)**:
1. **리더-부하 오케스트레이션 + `[Agent]` 라우팅 + reflection 요약** — talon의 시황/섹터/종목/차트 분석가 → 총괄 구조에 이식. 단 Claude Agent SDK로 재구현, 턴 수를 엄격히 상한.
2. **"전문지식 = 지시 주입 툴" 패턴**(`analyzer.py`): 툴이 분석을 수행하지 않고 *데이터 묶음 + 분석 지시 프롬프트*를 반환. 도메인 휴리스틱은 코드에, 추론은 LLM에 두는 깔끔한 분리. 한국 재무제표/차트 분석에 매우 재사용성 높음.
3. **선언적 역할 레지스트리**(`agent_library` 딕셔너리) — 에이전트를 config로 관리.
4. **backtrader를 백테스트 엔진으로 채택**(자체 구현보다 나음). 단 수수료/거래세/슬리피지/사이징/워크포워드를 반드시 추가.
5. **섹션별 Pydantic `output_type`**(equity 에이전트) — 텔레그램 리포트용 타입 안전 출력.

**참고만(reference)**: 8섹션 리포트 목차, valuation/sensitivity 엔진 스케치(단 US-GAAP 기준).

**피할 것(avoid)**:
1. **OpenAI Agents SDK / AutoGen 스택 상속** — 구독제 제약과 충돌. talon은 Claude Agent SDK 직결.
2. **LLM이 테스트 구간에서 전략을 반복 튜닝하는 루프** — 인샘플 과적합. talon은 워크포워드/OOS를 강제하고 LLM은 *가설 제안*만, 테스트 데이터 튜닝 금지.
3. **거래비용 0 백테스트** — 한국 거래세+슬리피지 미반영 결과는 폐기 대상.
4. **무제한 30턴 그룹챗** — Claude Max 주간 쿼터를 순식간에 소진. 경계가 명확한 결정론적 오케스트레이션 필요.
5. **DataFrame→문자열 삽입을 유일한 데이터 인터페이스로 쓰는 것** — 수치 근거 없이 환각 위험. 매매 판단용 지표는 코드로 계산해 컴팩트한 구조화 팩트로 전달.
6. **라이브/페이퍼 어댑터 부재** — 토스 연동에 참고할 자산 없음.

## 8. 유지보수 상태

논문 2024, 코어는 구형 AutoGen 0.2.x·구형 GPT-4 모델 고정. equity 모듈이 더 제품형(웹앱/DB)이나 OpenAI 락인. 데모·연구 품질과 제품형 코드가 혼재. 프로덕션 의존 대상으로는 부적합, **아이디어 소스**로만 유효.
