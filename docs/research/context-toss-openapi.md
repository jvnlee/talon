# 토스증권 OpenAPI 정밀 조사 리포트

> 작성일: 2026-07-09 · 대상 프로젝트: talon (개인 1인용 국내+미국 주식 단타·스윙 투자 에이전트)
> 1차 출처: 토스증권 공식 개발자센터 및 canonical OpenAPI 3.0 스펙(`openapi.json` v1.2.2)
> 조사 방법: 공식 문서·`llms.txt`·OpenAPI JSON 원본 파싱 + 경쟁 증권사 문서/기사 교차확인

---

## 0. 요약 (TL;DR)

- 토스증권 OpenAPI는 **REST 전용**(2026년 중순 사전신청 오픈, 아직 점진 롤아웃/베타 성격). 국내(KRX/NXT)와 **미국 주식 시세·주문을 단일 API로 통합** 제공. OAuth 2.0 Client Credentials 인증.
- **주문**: 지정가/시장가, 매수/매도, 정정/취소, **조건주문(SINGLE/OCO/OTO)**, **미국 소수점(금액기반) 주문** 지원. 멱등키·고액주문 방지 등 안전장치 내장.
- **talon 로드맵 관점의 3대 공백**: (1) **모의투자(페이퍼 트레이딩) API 없음**, (2) **실시간 WebSocket 없음(추후 지원 예정)** → REST 폴링만, (3) **분봉이 1분봉(1m)뿐** + 캔들 200개/요청 한도 → 백테스팅용 대량 히스토리·다중 타임프레임에 취약. **뉴스/공시 API도 없음**.
- **판단**: talon의 *실전 집행 계층*은 토스로 적합(사용자가 이미 키 보유, 국내+미국 통합, AI 에이전트 친화). 그러나 프로젝트가 요구하는 **광범위 백테스팅 → 수개월 페이퍼 트레이딩** 단계는 토스 단독으로 충족 불가. **한국투자증권(KIS Developers)을 보완 계좌로 병행**하는 하이브리드 구성을 권장(모의투자·미국 포함, WebSocket 실시간, 풍부한 분봉/과거데이터).

---

## 1. 공식 문서 및 접근 경로

| 항목 | 값 |
|---|---|
| 개발자센터(가이드) | `https://developers.tossinvest.com/docs` (JS 렌더링 SPA) |
| AI/에이전트용 문서 | `https://developers.tossinvest.com/llms.txt` |
| Canonical 스펙(진실의 원천) | `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json` (OpenAPI 3.0, **v1.2.2**) |
| API Base URL | `https://openapi.tossinvest.com` (단일 서버, **별도 sandbox/mock 서버 없음**) |
| 서비스 소개(홈) | `https://corp.tossinvest.com/ko/open-api`, `https://home.tossinvest.com/ko/open-api` |

- 표준 OpenAPI JSON이라 자동 SDK 생성 가능. AI 에이전트(Claude Code/Codex/Cursor 등) 연동을 명시적으로 겨냥해 `llms.txt`를 별도 제공하는 점이 특징 — **talon의 Claude Agent SDK 워크플로우와 궁합이 좋음**.

---

## 2. 인증 · 발급/이용 조건

- **인증 방식**: OAuth 2.0 **Client Credentials Grant**.
  - `POST /oauth2/token` — `Client ID:Secret`을 Base64 Basic 인증, `grant_type=client_credentials` → `access_token`(약 1시간 만료) 발급.
  - 모든 호출에 `Authorization: Bearer {access_token}` 헤더.
  - **계좌/자산/주문 계열 엔드포인트는 추가로 `X-Tossinvest-Account` 헤더**(값: `/api/v1/accounts`가 반환하는 `accountSeq`) 필요.
  - (참고) 스펙 v1.2.2 paths에는 JWKS 엔드포인트가 노출돼 있지 않음. 토큰 발급은 `/oauth2/token` 단일 경로.
- **발급/이용 조건**:
  - **토스증권 계좌 보유자면 누구나** 신청 가능(계좌는 앱에서 비대면 개설).
  - 신청은 **토스증권 앱 → 더보기 → Open API → 신청**, 약관 동의 후 **Client ID/Secret 발급**. 키는 **앱 내에서만 재확인** 가능.
  - **사용자는 이미 API Key를 보유**(talon 제약 조건에 명시). 별도 심사/법인 요건 없이 개인 사용 가능.

---

## 3. 제공 API 전체 목록 (canonical 스펙 기준, 30개 오퍼레이션 / 13개 카테고리)

### 3.1 Auth
- `POST /oauth2/token` — 액세스 토큰 발급.

### 3.2 Market Data (시세)
- `GET /api/v1/orderbook` — **호가** 조회(매수/매도 호가 및 잔량).
- `GET /api/v1/prices` — 현재가 조회(**최대 200종목** 콤마 구분 동시 조회).
- `GET /api/v1/trades` — 최근 체결 내역(최대 50건).
- `GET /api/v1/price-limits` — 상/하한가.
- `GET /api/v1/candles` — **캔들(OHLCV)**. `interval` ∈ **{`1m`, `1d`}** 뿐. `count` 기본 100 / **최대 200**. `before`(ISO8601) 커서 페이지네이션(`nextBefore`). `adjusted`(수정주가, 기본 true).

### 3.3 Stock Info
- `GET /api/v1/stocks` — 종목 기본정보(이름/시장/통화/상태, 최대 200종목).
- `GET /api/v1/stocks/{symbol}/warnings` — 매수 유의사항(청산·과열·VI 발동 등 경고 플래그).

### 3.4 Market Info
- `GET /api/v1/exchange-rate` — 원/달러 환율(1분 갱신, **참고용**).
- `GET /api/v1/market-calendar/KR` — 국내 장 운영정보(**KRX+NXT 통합**, preMarket/regularMarket/afterMarket 세션).
- `GET /api/v1/market-calendar/US` — **미국** 장 운영정보(dayMarket/preMarket/regularMarket/afterMarket, KST 기준).

### 3.5 Ranking
- `GET /api/v1/rankings` — 주식 랭킹. type ∈ {거래대금, 거래량, 급등(TOP_GAINERS), 급락(TOP_LOSERS), 토스증권 거래대금/거래량}. market ∈ {KR, US}. duration ∈ {realtime, 1d, 1w, 1mo, 3mo, 6mo, 1y}.

### 3.6 Market Indicators (지수·금리·수급)
- `GET /api/v1/market-indicators/prices` — 8개 지표(KOSPI, KOSDAQ, 국고채 2/3/5/10/20/30년).
- `GET /api/v1/market-indicators/{symbol}/candles` — 지표 캔들(interval `1m`/`1d`).
- `GET /api/v1/market-indicators/{symbol}/investor-trading` — **투자자별 매매대금(수급 데이터)**. 개인/외국인/기관(세부 7분류)/기타법인. interval ∈ {1d, 1w, 1mo, 1y}. **KOSPI/KOSDAQ만**.

### 3.7 Account / Asset
- `GET /api/v1/accounts` — 계좌 목록(`accountSeq` 획득).
- `GET /api/v1/holdings` — 보유 주식(**국내+미국 통합**, 수량/평단/평가금액/손익, KRW 환산).

### 3.8 Order (주문)
- `POST /api/v1/orders` — **주문 생성**(지정가/시장가).
- `POST /api/v1/orders/{orderId}/modify` — **정정**.
- `POST /api/v1/orders/{orderId}/cancel` — **취소**.

### 3.9 Order History / Order Info
- `GET /api/v1/orders` — 주문 목록(status ∈ OPEN/CLOSED).
- `GET /api/v1/orders/{orderId}` — 주문 상세 + 체결내역.
- `GET /api/v1/buying-power` — 매수가능금액.
- `GET /api/v1/sellable-quantity` — 판매가능수량.
- `GET /api/v1/commissions` — 매매 수수료.

### 3.10 Conditional Order (조건주문)
- `POST /api/v1/conditional-orders` — 조건주문 생성. type ∈ **{SINGLE, OCO, OTO}**, `triggerPrice`(감시가) 도달 시 트리거, `expireDate`(만료일) 필수. **OCO/OTO는 지정가(LIMIT)만**.
- `POST /api/v1/conditional-orders/{id}/modify`, `DELETE /api/v1/conditional-orders/{id}` — 수정/취소.
- `GET /api/v1/conditional-orders`, `GET /api/v1/conditional-orders/{id}` — 목록/상세.

### 3.11 제공하지 않는 것 (중요)
- ❌ **실시간 WebSocket 스트리밍** — 스펙에 "웹 소켓은 추후 지원 예정입니다" 명시. 현재 실시간은 REST 폴링뿐.
- ❌ **모의투자/페이퍼 트레이딩 API** — sandbox 서버·mock 엔드포인트 없음(앱 내 별도 모의투자 기능은 API 미연동).
- ❌ **뉴스/공시 API** — 제공 안 함.
- ❌ **파생상품(선물·옵션), 미국 외 해외시장(일/중/홍/베)** — Open API 범위 밖(주식 위주, 해외는 US만).
- ❌ 다중 분봉(3/5/15분, 1시간) — 캔들 intraday는 **1m 단일**.

---

## 4. 주문(Order) 상세 규격 (canonical 스펙 필드 분석)

- **`orderType`**: `LIMIT`(지정가) | `MARKET`(시장가).
- **`side`**: `BUY` | `SELL`.
- **`timeInForce`**: `DAY`(당일) | `CLS`(장마감, At-the-Close). **`LIMIT`+`CLS` = LOC**. **CLS는 미국 주식 + LIMIT 조합만** 지원. → 그 외 별도의 프리마켓/애프터마켓 주문 유형은 API에 노출되지 않음(정규장 중심).
- **`symbol`**: KRX는 6자리 숫자(예 `005930`), US는 영문 티커(예 `AAPL`).
- **주문 방식 두 가지 (oneOf)**:
  1. **수량기반(Quantity)**: `quantity` 지정. 기본 양의 정수. **소수점 수량은 미국 주식 시장가 매도(`MARKET`+`SELL`)만** 허용, **정규장 시간에만**, 소수점 6자리까지.
  2. **금액기반(Amount)**: `orderAmount`(달러) 지정. **미국 주식 시장가(`MARKET`) 전용** — 소수점 **매수** 용도. 정규장 시간에만.
- **`price`**: `LIMIT`일 때 필수(`MARKET`이면 금지). KR은 정수(원, **호가단위 준수** 필수), US는 소수(<$1 넷째자리, ≥$1 둘째자리).
- **안전장치**: `clientOrderId`(멱등키, 최대 36자, **10분 유효**) / `confirmHighValueOrder`(**1억원 이상 주문 시 true 필수**, **30억원 이상은 무조건 거부** `max-order-amount-exceeded`).
- **정정 제약**: KR은 `quantity` 필수(정수), **US는 수량 변경 불가**(`us-modify-quantity-not-supported`).

> talon 함의: 조건주문(OCO/OTO)·멱등키·시장가 금액주문 등 **자동매매 봇에 필요한 원자적 주문 프리미티브가 이미 갖춰짐**. 소수점 매수(금액기반)로 1000만원 시드의 미국 고가주 분할매수도 가능.

---

## 5. Rate Limit

- **토큰 버킷(burst) 방식**, **엔드포인트 그룹별 개별 한도**. 그룹: `AUTH`, `MARKET_DATA`, `MARKET_DATA_CHART`(캔들은 부하 특성이 달라 분리), `STOCK`, `MARKET_INFO`, `RANKING`, `MARKET_INDICATOR`, `MARKET_INDICATOR_CHART` 등.
- 한도는 **응답 헤더로 전달**: `X-RateLimit-Limit`(현재 허용 **초당** 요청 수 = burst capacity, 스펙 예시값 **10**), `X-RateLimit-Remaining`(잔여 토큰, 429 시 0), `X-RateLimit-Reset`(토큰 1개 재충전까지 예상 초). 초과 시 **HTTP 429 `rate-limit-exceeded`**.
- 고정 공표 수치는 스펙에 없음(동적/헤더 기반). 캔들 차트와 일반 시세는 **별도 그룹**이라 각각 헤더를 확인해야 함.

> talon 함의: 실시간 스트리밍이 없으므로 단타는 REST 폴링(초당 ~10 burst)에 의존 → 다종목 초단위 감시에는 반응성·호출예산 제약. 폴링 스케줄러에 그룹별 헤더 기반 백오프 구현 필요.

---

## 6. 미국 주식 / 모의투자 / 봇 약관

- **미국 주식 주문**: ✅ 지원. 시세·주문·보유·랭킹·장운영 캘린더 모두 US 포함. 소수점(금액기반) 주문은 US 전용 강점.
- **모의투자(페이퍼 트레이딩) API**: ❌ **없음**. 실계좌 소액(1주 등) 테스트 권장이 공식 가이드. → talon의 "수개월 실시장 페이퍼 트레이딩" 요구를 토스 API로 직접 충족 불가.
- **자동매매/봇 약관**: 별도의 봇 금지 조항은 확인되지 않았고, 오히려 **상품 자체가 자동매매·시스템 트레이딩·AI 에이전트를 겨냥**(마케팅: "새벽 자동매매", Claude Code/Codex 연동, `llms.txt` 제공). API 주문도 **일반 거래와 동일 수수료**. 다만 **투자유의종목은 추가 동의/차단**될 수 있고, Client Secret 노출 금지 등 일반적 보안 의무 존재. 정식 약관 전문은 앱 내 동의 화면에서 확인 필요.
- **롤아웃 상태**: 2026년 5월 중순 사전신청 오픈 → 2주 만에 5.5만명 신청. **안정화 우선 필수기능만 우선 개방, 하반기 "완성형 서비스" 목표**로 단계적 확대(WebSocket 등 추가 예정). 즉 **아직 성장 중인 베타 성격** — 스펙 변경 가능성 유의.

---

## 7. 경쟁 증권사 비교 (모의투자 · 미국 주식 중심)

| 항목 | **토스증권** | **한국투자증권(KIS)** | **LS증권(구 이베스트)** | **키움증권 REST** |
|---|---|---|---|---|
| REST API | ✅ | ✅ | ✅ | ✅ |
| 실시간 WebSocket | ❌ (추후 예정) | ✅ | ✅ | ✅ |
| 국내 시세·주문 | ✅ | ✅ | ✅ | ✅ |
| **미국 주식 시세** | ✅ | ✅ | ✅ | ❌ (국내 전용) |
| **미국 주식 주문** | ✅ (소수점 강점) | ✅ | ✅ | ❌ |
| 미국 외 해외(일/중/홍/베) | ❌ | ✅ | 일부 | ❌ |
| **모의투자(페이퍼)** | ❌ | ✅ (실전/모의 도메인 전환, **US 포함**) | ✅ (모의 서버 옵션)¹ | ✅ (국내) |
| Intraday 분봉 | **1m만** | 다양(분/일/주/월) | 다양 | 다양 |
| 파생(선·옵) | ❌ | ✅ | ✅ | 해외파생 별도 |
| 조건주문(OCO/OTO) | ✅ | 제한적² | 제한적² | 제한적² |
| 수급(투자자별) 데이터 | ✅ | ✅ | ✅ | ✅ |
| Rate limit 성격 | 그룹별 토큰버킷(초당 ~10 burst) | 실전 대략 초당 20건 내외(모의는 더 낮음)² | 초당 제한(문서 상세)² | TR별 제한² |
| 개발 편의/AI 친화 | **최상**(표준 OpenAPI+`llms.txt`) | 높음(공식 GitHub/샘플 풍부) | 높음(문서 상세) | 중간 |

¹ LS 모의투자의 **해외주식 커버리지 세부**는 공식 문서 확인 권장(불확실).
² 정확한 수치·지원범위는 각 사 공식 문서 확인 필요(본 표는 교차확인된 정성 비교).

**핵심 시사점**
- **키움 REST**는 현재 **미국 주식 미지원(국내 전용)** → 미국이 필수인 talon엔 보조로도 부적합.
- **KIS**는 **모의투자에서 미국 주문까지 지원**(env로 실전/모의 전환)하고 WebSocket·다중 분봉·넓은 해외 커버리지를 갖춰 **페이퍼 트레이딩·백테스팅·실시간 단계의 공백을 정확히 메움**. 단, 모의계좌는 호출 한도가 실전보다 낮음.
- **LS**는 US·모의·WebSocket을 모두 갖춘 KIS 대안 후보이나, 문서·해외 커버리지·생태계는 KIS가 우위.

---

## 8. talon 적합성 판단 및 권고

### 토스 단독으로 충분한가? → **아니오 (현 시점). 하지만 실전 집행에는 최적.**

**토스 채택이 타당한 영역 (실전 매매 집행 계층)**
- 사용자가 이미 키 보유, 국내+미국 단일 통합, OAuth 심플, 표준 OpenAPI+`llms.txt`로 **Claude Agent SDK 워크플로우와 최고 궁합**.
- 조건주문(OCO/OTO), 멱등키, 시장가 금액주문(소수점 매수), 매수가능금액/판매가능수량/수수료 조회 등 **주문 집행에 필요한 프리미티브 완비** → 단타/스윙 실전 주문 실행에 부족함 없음.
- 랭킹·수급(투자자별 매매대금)·지수/금리 지표 등 **퀀트 시그널 재료도 일부 기본 제공**.

**토스 단독으로 불가능한 영역 (talon 초기 검증 로드맵의 핵심)**
1. **모의투자 API 부재** → "수개월 실시장 페이퍼 트레이딩"을 토스 API로 못 함. 자체 페이퍼 엔진을 토스 시세 위에 구현하거나 외부 모의계좌 필요.
2. **실시간 WebSocket 부재** → 데이트레이딩용 실시간 체결/호가 스트림 없음. REST 폴링(초당 ~10 burst) 의존 → 다종목 초단위 반응성 제약.
3. **분봉 1m 단일 + 캔들 200개/요청** → 5/15분봉은 1분봉 자체 집계로 만들 수 있으나, **광범위 백테스팅용 장기 히스토리 대량 수집엔 부적합**(rate limit·페이지 크기 제약).
4. **뉴스/공시 API 부재** → 에이전트의 "뉴스 리서치" 기능은 외부 소스(공시 DART, 뉴스 API 등)로 별도 조달.

### 권고 아키텍처 (하이브리드)
- **집행/에이전트 UX 계층 = 토스증권 OpenAPI** (사용자 실계좌, 국내+미국 통합 주문, AI 친화).
- **검증/데이터 계층 = 한국투자증권(KIS Developers) 보완 계좌**:
  - **모의투자(미국 포함)** 로 수개월 페이퍼 트레이딩 수행.
  - **WebSocket 실시간** 으로 단타 반응성 확보.
  - **다중 분봉·풍부한 과거데이터** 로 백테스팅 데이터 파이프라인 구축.
- 브로커 추상화 레이어를 두어 talon 내부에서 **KIS(모의/데이터/실시간) ↔ 토스(실전 집행)** 를 스위칭. **최종 실전·자동매매 단계에서 토스로 수렴**하거나, **토스 WebSocket 정식 출시(하반기 목표) 시 재평가**하여 단일화 검토.
- 뉴스/공시는 브로커 API 밖 외부 소스로 별도 통합.

---

## 부록 A. 주요 출처
- 토스증권 개발자센터 가이드: https://developers.tossinvest.com/docs
- 토스증권 `llms.txt`: https://developers.tossinvest.com/llms.txt
- **Canonical OpenAPI 스펙(v1.2.2, 본 리포트 1차 근거)**: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
- 토스증권 Open API 소개: https://corp.tossinvest.com/ko/open-api
- 토스 Open API 완벽 가이드(2026): https://www.pulse-know.com/toss-invest-open-api-guide-2026/
- 롤아웃 기사(5.5만명/하반기 완성형): https://marketin.edaily.co.kr/News/ReadE?newsId=04795366645480736
- 자동매매 기사: https://v.daum.net/v/20260521073602305
- KIS Developers: https://apiportal.koreainvestment.com/intro · https://github.com/koreainvestment/open-trading-api
- LS증권 Open API: https://openapi.ls-sec.co.kr/intro
- 키움 REST API: https://openapi.kiwoom.com/main/home
- 증권 API 비교: https://govapi.kr/securities-api-automated-trading-guide/

## 부록 B. 미해결/확인필요 사항
- 토스 그룹별 **정확한 rate limit 수치**(현재는 응답 헤더로만 동적 제공, 예시 초당 10 burst).
- 토스 **WebSocket 정식 출시 시점**(2026 하반기 "완성형" 목표로만 공지) 및 **모의투자/뉴스 API 추가 여부**.
- 토스 자동매매에 대한 **약관 전문**(앱 내 동의 화면) — 명시적 봇 제한 조항 유무 최종 확인.
- KIS/LS **모의투자의 해외(미국) 실시간 시세·주문 세부 제약**(모의는 실전 대비 기능·한도 축소 가능).
