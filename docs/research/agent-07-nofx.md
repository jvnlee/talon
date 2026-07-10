# NOFX 정밀 분석 리포트 (talon 설계 관점)

- 대상 레포: `NoFxAiOS/nofx` (AGPL-3.0, Go 백엔드 ~76K LOC + React/TS 프론트)
- 최신 버전: v3.0.0 "Major Architecture Transformation" (2025-10-30), 이후에도 활발히 유지보수 중
- 한 줄 요약: **"전략이 곧 LLM"**인 암호화폐 무기한선물(perp) 자동매매 터미널. LLM이 매 사이클마다 시장을 읽고 JSON 주문을 제안하면, Go 런타임이 코드로 박아둔 하드 리스크 한도로 클램프한다.

## 1. 시스템 구조

핵심은 두 개의 독립된 LLM 시스템이다.

**(A) 자율 트레이더 루프** (`kernel/` + `trader/`) — 실제 매매 두뇌.
- `trader/auto_trader_loop.go:runCycle()`가 N분마다 1사이클 실행: 컨텍스트 수집 → 단일 LLM 호출 → JSON 파싱 → 정렬(청산 우선) → 스로틀/리스크 클램프 → 주문 → DB 기록.
- `kernel/engine_analysis.go:GetFullDecisionWithStrategy()`가 시스템+유저 프롬프트를 만들어 **단 한 번** `CallWithMessages()`로 호출한다. **멀티에이전트 토론·합의·투표 구조는 없다.** "롱/숏 커버리지 강제", "그리드 엔진"은 모두 결정론적 Go 코드이지 별도 LLM이 아니다.

**(B) NOFXi 텔레그램 운영 비서** (`telegram/agent/`, `agents.md`) — 매매가 아니라 트레이더/거래소/모델/전략 **설정·진단 대화**를 담당. `80% skill + 20% 동적 플래닝` 철학의 ReAct 플래너이며, 3계층 메모리(`chatHistory`/`TaskState`/`ExecutionState`)를 쓴다. talon의 텔레그램 봇 인터페이스에 직접 참고 가치가 있다.

## 2. 데이터 파이프라인

- OHLCV: Binance 공개 klines(`market/historical.go`, `data_klines.go`). 지표(EMA/MACD/RSI/ATR/Volume)는 로컬 계산.
- 시그널·랭킹: 자체 유료 데이터 스택 — Claw402/Vergex/Coinank(`provider/nofxos`, `provider/vergex`, `provider/coinank`)에서 AI500 코인 레이팅, OI 증감, 넷플로우(기관/개인), 청산 히트맵을 가져온다.
- 종목 유니버스 선택(`kernel/engine.go:GetCandidateCoins`): static / ai500 / oi_top / mixed / vergex_signal 모드. **이 부분이 talon의 팩터·스크리너 후보군 선택과 개념적으로 대응**된다(단, 소스는 암호화폐 전용).
- US 주식은 Alpaca/TwelveData를 **데이터 피드로만** 쓰고, 실제 체결은 Hyperliquid의 토큰화 주식 perp(TSLA·NVDA·SPX·OPENAI 등)로 한다. 실주식 브로커리지가 아니다.

## 3. LLM 오케스트레이션 (핵심)

- **역할 분담 없음**: 단일 에이전트가 계좌·포지션·후보종목을 한꺼번에 분석. 프롬프트 8섹션(역할 정의 / 트레이딩 모드 변형 aggressive·conservative·scalping / 하드 제약 / 빈도 / 진입 기준 / 결정 프로세스 / 출력 포맷 / 커스텀). 출력은 `<reasoning>`(CoT) + `<decision>`(JSON 배열) 형식.
- **강건한 파싱**(`engine_analysis.go`): 보이지 않는 유니코드 제거, 중국어 문장부호→ASCII 교정, ```json 펜스 정규식 추출, 스키마 검증, 리스크 파라미터 검증까지. LLM 출력의 지저분함을 코드로 방어하는 실전 노하우가 응축돼 있다.
- **메모리/반성(reflection) 구조**: 무거운 reflection 루프가 아니라, 프롬프트에 **최근 청산 10건 + 러닝 통계(승률·PF·Sharpe·MaxDD) + 포지션별 Peak PnL**을 주입하는 경량 방식(`buildTradingContext`). 저비용으로 "자기 실적을 보고 판단"하게 만든다.
- **토큰 가드**: 호출 전 `EstimateTokens()`로 모델별 컨텍스트 한도와 비교해 초과 시 호출 자체를 차단하거나 유니버스를 줄인다.
- **비용 구조**: 8개 프로바이더 전부 **BYO API 키 또는 Claw402(x402 프로토콜, USDC 마이크로페이먼트)**의 **호출당 종량 과금**이다. `store/ai_charge.go`는 호출당 단가를 박아둔다(DeepSeek $0.003 ~ claude-opus $0.12/call). 15분 주기면 하루 96콜 → DeepSeek 약 $0.3/일, claude-opus 약 $11.5/일. **구독 인증(Claude Code/Agent SDK OAuth) 경로는 존재하지 않는다** — 전부 OpenAI 호환 HTTP + Bearer 키다.

## 4. 백테스팅 엔진 — **없다**

레포 전체에 backtest / vectorized / event-driven 백테스터, 룩어헤드 방지, 체결 시뮬레이션, 수수료·슬리피지 모델이 **전무**하다(`slippage` 언급은 라이브 지정가 오프셋뿐). 검증 방식은:
1. 소액 실전으로 돌리며 **학습을 코드 상수에 박는다.** 예: `auto_trader_throttle.go`에 `autopilotMinHoldDuration = 60m` 옆 주석 "*트레이딩을 1시간 미만 보유하면 수수료 감안 시 순손실이었다*". 즉 **라이브 트라이얼-앤-에러가 백테스트의 대용품**이다.
2. Vergex 공개 리더보드로 여러 모델의 **실현수익률을 라이브 비교**.

이는 talon 관점에서 **경계 대상**이다. talon은 광범위한 백테스팅을 명시적 요구사항으로 두므로, NOFX는 "백테스터를 안 만들면 어떻게 되는가(과적합된 매직넘버, 최근 라이브에 편향된 상수)"의 반면교사다.

## 5. 리스크 관리 (가장 배울 점)

**"모델은 제안, 런타임은 처분(The model proposes, the runtime disposes)"** — LLM이 건드릴 수 없는 코드 클램프를 주문 경로에 강제한다(`trader/auto_trader_risk.go`, `auto_trader_throttle.go`):
- 최대 동시 포지션 수, 자기자본 대비 명목가치 비율 캡(알트 1x / BTC·ETH·XYZ 5x), 레버리지 하드캡, 심볼당 1포지션.
- 진입 즉시 거래소 측 SL/TP 설정, 드로다운 자동청산(수익 >5%인데 고점 대비 40% 반납 시).
- 스로틀: 최소 보유 60분, 재진입 쿨다운 30분, 사이클당·시간당 진입 상한.
- 세이프 모드: LLM 3회 연속 실패 시 신규 진입 차단(기존 포지션은 SL로 보호). 실행 전 프리플라이트(모델 접근·지갑 잔액·전략·거래소 잔액 검증).
- 모든 결정은 시스템/유저 프롬프트·CoT·원문 응답·실행 로그와 함께 DB에 감사 추적(audit trail) 저장 — "근거 없는 포지션은 없다".

## 6. 라이브 트레이딩 어댑터

`trader/{binance,bybit,okx,hyperliquid,...}`에 거래소별 어댑터가 인터페이스(`trader/interface.go`)로 추상화. 지정가/시장가, 포지션 동기화(`exchange_sync`, `position_rebuild`), 체결 확인 폴링(5회×500ms). 자격증명은 암호화 저장(`crypto/`, `ENCRYPTION_README.md`). 토스증권 어댑터로 치환할 자리는 인터페이스 수준에서만 참고 가능하나, 현물 주식과 무기한선물은 주문/포지션 모델이 달라 재사용성은 낮다.

## 7. talon 적용성 평가

### 그대로 채택 → 없음
암호화폐 perp·레버리지·청산·펀딩레이트 도메인이 KR+US 현물 주식과 근본적으로 다르고, LLM 플러밍이 종량 과금 전제라 구독제 talon과 충돌한다.

### 패턴만 빌릴 것 (borrow) — 가치 높음
1. **"모델 제안 / 런타임 클램프" 이중화**: talon이 초기엔 사람이 최종 주문하더라도, 넘을 수 없는 리스크 가드(포지션 한도·손실 컷·쿨다운·세이프 모드)를 코드로 박아두는 설계는 그대로 가져갈 최상위 교훈.
2. **단일샷 구조화 결정 프롬프트 + 강건 파싱**: 시스템(역할+하드제약+가이드값+출력 스키마) + 유저(컨텍스트) → `<reasoning>`+`<decision>` JSON. 파싱 방어 로직. talon의 "매매 근거 제시"에 직접 이식 가능하며, **구독제(호출 예산 제약)에 가장 적합한 저비용 구조**다.
3. **결정 감사 추적**: 프롬프트·CoT·응답·실행결과 전량 저장. talon의 수개월 페이퍼 트레이딩 검증 단계에 필수.
4. **경량 메모리**: reflection 루프 대신 "최근 N체결 + 러닝 통계 + Peak PnL"을 프롬프트에 주입. 구독 호출 예산 안에서 효율적.
5. **호출 전 토큰 예산 가드**, **연속 실패 서킷브레이커·프리플라이트**.
6. **NOFXi 텔레그램 비서의 skill-first + 3계층 메모리**: talon 텔레그램 봇을 "표준 작업은 skill로 고정, 미지의 것만 동적 플래닝"으로 설계하는 청사진.

### 참고만 (reference)
- 유니버스 선택(ai500/oi_top/mixed) 구조 → talon 팩터/스크리너 후보군 파이프라인의 형태적 참고.
- "라이브 상수 튜닝 = 백테스트 대용" → **하지 말 것의 예시**. talon은 NOFX가 결여한 이벤트 기반 백테스터(체결·수수료·슬리피지·룩어헤드 방지 포함)를 반드시 별도 구축해야 한다.

### 피할 것 (avoid)
- 종량 과금 LLM 플러밍(`mcp/`) 전체 — 구독 인증 경로 부재.
- 레버리지/청산/펀딩 중심 리스크 로직의 무비판적 이식.

### 한국 시장 적용성
사실상 없음. KRX/KOSPI/토스 연동 부재(`KOREA-200`/`EWY`는 Hyperliquid 토큰화 티커일 뿐). 현물 주식·한국 장 구조(가격제한폭, 동시호가 등)에 대한 고려 전무.

### 구독제 LLM 제약과의 궁합
**나쁨.** NOFX는 태생부터 per-call 과금·마이크로페이먼트 최적화 설계다. talon은 아키텍처 패턴(리스크 런타임·구조화 프롬프트·감사 추적·경량 메모리·텔레그램 skill 설계)만 취하고, LLM 호출 계층은 Claude Code/Agent SDK 구독 인증 위에 자체 구현해야 한다.

## 최종 판정: **borrow**
리스크 런타임 이중화, 구조화 결정 프롬프트, 감사 추적, 경량 메모리, 텔레그램 skill-first 설계를 빌리되 — 암호화폐/레버리지 도메인, 종량 과금 LLM 플러밍, 그리고 "백테스터 없이 라이브 상수로 때우는 문화"는 피한다.
