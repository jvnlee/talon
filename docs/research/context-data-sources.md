# talon 시장 데이터 소스 지형도 (KR/US)

> 조사일: 2026-07-09
> 대상: talon(개인 투자자 1인용 KR+US 주식 단타/스윙 에이전트)
> 목적: 시황·섹터·종목 리서치, 지표/차트 분석, 퀀트 백테스팅에 쓸 데이터 소스 후보군 정리
> 제약 재확인: LLM은 구독제(Claude Max / Codex Pro)만 사용, 데이터 소스는 무료·저비용 우선. 브로커리지/시세는 토스증권 OpenAPI 보유.

---

## 0. 한눈 요약 (talon 권장 스택)

| 용도 | KR 1순위 | US 1순위 | 성격 |
|---|---|---|---|
| 실거래(주문/잔고/시세) | **토스증권 OpenAPI** | **토스증권 OpenAPI**(미국주식 지원) | 공식, 계좌 연동 |
| 일봉 대량(백테스팅) | **pykrx + FinanceDataReader** | **FinanceDataReader / yfinance / Stooq** | 무료, 스크래핑 |
| 분봉 대량(백테스팅) | 토스 candles / KIS OpenAPI(누적) / KRX Data Marketplace(유료) | **Alpaca(무료 10년+ 1분봉)** | KR은 난제, US는 Alpaca가 정답 |
| 공시/재무 | **OpenDART** | **SEC EDGAR** | 공식, 무료 |
| 거시/경제지표 | **한국은행 ECOS** | **FRED** | 공식, 무료 |
| 뉴스 | **네이버 검색 API** + 언론사 RSS | **GDELT** + Finnhub/Marketaux | 무료 티어 |

핵심 결론 3가지:
1. **US 분봉 백테스팅은 Alpaca 무료 티어가 사실상 정답**(10년+ 1분봉, 200 req/min). KR 분봉은 무료로 대량 확보할 정공법이 없어 브로커 API로 실시간 누적하거나 KRX/코스콤 유료 데이터를 사야 한다 — talon 설계상 가장 큰 데이터 리스크.
2. **일봉·공시·거시·뉴스는 KR/US 모두 무료로 충분히 커버 가능**. 실시간 시세는 토스 OpenAPI(공식 계좌 연동)로 일원화.
3. **qlib에 붙은 공식 한국 어댑터는 없음** — pykrx/FDR로 받은 데이터를 qlib bin 포맷으로 직접 덤프하는 DIY가 유일한 경로.

---

## 1. 한국 시장 데이터

### 1.1 KRX 정보데이터시스템 / KRX Data Marketplace
- **URL**: `data.krx.co.kr`
- **커버리지**: KOSPI/KOSDAQ/KONEX 전종목 일별 시세, 지수, 공매도, 투자자별 매매동향, 파생 등 원천(공식) 데이터.
- **무료 여부**: 웹 화면 조회·CSV 다운로드는 무료. 단, **분/틱 단위 과거 데이터, 대량 히스토리컬 데이터셋은 KRX Data Marketplace / 코스콤(KOSCOM)에서 유료 판매**.
- **실시간/지연**: 웹 공개 데이터는 EOD(장 마감 후) 기준, 실시간 아님.
- **안정성**: 원천 데이터라 정확도 최상. 단 공식 REST OpenAPI가 빈약해 대부분 pykrx 등이 이 사이트를 스크래핑하는 구조 → 사이트 개편 시 하위 라이브러리 동반 파손.
- **talon 활용**: 정합성 검증(pykrx 값 크로스체크)용 기준 소스. 대량 분봉이 꼭 필요하면 유료 마켓플레이스 검토.

### 1.2 pykrx
- **성격**: KRX + 네이버금융을 스크래핑하는 파이썬 라이브러리(`pip install pykrx`). 비공식.
- **커버리지**: 일별 OHLCV, 전종목 시세, 지수(KOSPI/KOSDAQ/KRX 계열), **투자자별 수급(기관/외인/개인)**, 공매도, **펀더멘털(PER/PBR/배당수익률)**, 상장종목 리스트.
- **무료/안정성**: 무료. 스크래핑 기반이라 KRX 사이트 구조 변경 시 함수가 갑자기 죽는 문제가 주기적으로 발생. 대량 조회는 느림(1종목 20년치 ≈ 1분).
- **실시간/지연**: EOD 일봉 중심. **분봉은 안정적으로 제공하지 못함**(실질적으로 일봉 도구로 봐야 함).
- **talon 활용**: KR 일봉/수급/펀더멘털 팩터 확보의 주력. `pykrx-mcp`(MCP 서버) 버전도 존재해 에이전트 연동 용이.

### 1.3 FinanceDataReader (FDR)
- **성격**: KR+US+글로벌을 한 인터페이스로 묶은 스크래핑 래퍼(`pip install finance-datareader`). 출처는 네이버·야후·KRX.
- **커버리지**: KRX(KOSPI/KOSDAQ/KONEX) 종목·**상장폐지 리스트(KRX-DELISTING)**·관리종목·**한국 ETF 전종목**, 미국(NYSE/NASDAQ/S&P500) 종목·리스트, 글로벌 지수(DJI/IXIC/SSEC/HSI/N225 등), 환율, 암호화폐.
- **무료/안정성**: 무료. pykrx보다 넓은 범위(특히 US·글로벌·상폐 리스트)가 강점. 스크래핑 취약성은 동일.
- **실시간/지연**: **일봉 중심**(분봉 지원 명확치 않음, 사실상 일봉 도구).
- **talon 활용**: 종목 유니버스 구성(상폐 포함으로 생존편향 완화), KR/US 일봉 통합 파이프라인의 뼈대.

### 1.4 OpenDART (전자공시 OpenAPI, 금융감독원)
- **URL**: `opendart.fss.or.kr`
- **커버리지**: 공시 목록/원문, 기업개황, **정기보고서 재무제표(단일/다중회사, XBRL 기반)**, 지분공시(대량보유·임원 소유), 주요사항보고 등.
- **무료 여부**: 완전 무료. 회원가입 후 인증키 즉시 발급(누구나).
- **한도**: 개인 인증키 기준 **일 20,000회** 호출 제한이 통상 기준(기관 신청 시 일부 항목 무제한). 응답은 JSON/XML.
- **안정성**: 정부 공식 API로 안정적. 파이썬 래퍼 `OpenDartReader`, `dart-fss` 성숙.
- **talon 활용**: 공시 이벤트 트리거(유상증자/실적/최대주주 변경 등), 펀더멘털 팩터의 재무 원천.

### 1.5 네이버금융 (비공식 스크래핑)
- **성격**: 공식 API 없음. HTML/모바일 엔드포인트 스크래핑.
- **커버리지**: 실시간에 가까운 호가/체결, **일부 분봉**, 종목 토론/뉴스, 컨센서스 등 폭넓음.
- **무료/안정성/법적**: 무료지만 **가장 취약**(구조 변경·차단·robots/약관 회색지대). 상업적/대량 이용은 법적 리스크.
- **talon 활용**: 최후의 보조 수단. 핵심 파이프라인을 여기 의존시키지 말 것(pykrx/FDR가 이미 이걸 감싸고 있음).

### 1.6 한국은행 ECOS (경제통계 OpenAPI)
- **URL**: `ecos.bok.or.kr/api`
- **커버리지**: 기준금리·시장금리, 원/달러 등 환율, 통화량(M1/M2), 물가, 국민계정, 외환보유액 등 거시 시계열.
- **무료 여부**: 무료. 회원가입 시 인증키 자동 발급(대개 1일 내 활성화). 인증키 없이도 상위 100개 지표 중 10건 샘플 조회 가능.
- **한도**: 공식적으로 엄격한 rate limit 명시는 약함(통상 일 1만 건 수준으로 알려짐).
- **안정성**: 한국은행 공식, 안정적. 래퍼 `PublicDataReader`.
- **talon 활용**: KR 매크로 레짐(금리/환율/유동성) 피처. 시장 국면 판단, 리스크 온·오프 시그널.

---

## 2. 미국 시장 데이터

### 2.1 yfinance
- **성격**: 야후 파이낸스 비공식 스크래핑(`pip install yfinance`).
- **커버리지**: 글로벌 일봉, 인트라데이(1분봉은 최근 ~7~30일, `<1d` 간격은 최대 60일 룩백 제약), 재무제표·배당·옵션 체인.
- **무료/안정성**: 무료지만 **신뢰성 지속 악화**. 2024~2025년 야후가 rate limit을 강화해 `Ticker.info`조차 429(Too Many Requests) 빈발, 엔드포인트/인증 방식 수시 변경. IP 로테이션·캐싱·백오프 필수.
- **talon 활용**: 프로토타이핑/보조. 프로덕션 신뢰 소스로는 부적합.

### 2.2 Alpha Vantage
- **커버리지**: 글로벌 주식 일봉/인트라데이, 기술지표, FX, 암호화폐, **뉴스&센티먼트(200k+ 티커)**, 펀더멘털.
- **무료 한도**: **일 25회 + 분당 5회**로 대폭 축소됨(과거 500/일 → 100/일 → 현재 25/일). 무료로는 사실상 소량 조회만 가능. 유료 $50/월~.
- **안정성**: 공식 키 기반이라 안정적이나 무료 한도가 너무 빡빡.
- **talon 활용**: 뉴스 센티먼트 스팟 조회 정도. 대량 시세용으로는 부적합.

### 2.3 Polygon.io
- **무료 티어**: **분당 5콜, 2년치 히스토리, EOD(무료는 15분 지연)**. 무료로 일/분봉 히스토리 접근 가능(단 콜수 제약).
- **유료**: Stocks Starter $29/월(무제한 콜, 실시간은 상위 티어). 데이터 품질 우수.
- **talon 활용**: 무료로는 소규모 검증용. 향후 US 실시간/틱이 필요해지면 유료 1순위 후보.

### 2.4 Finnhub
- **무료 한도**: **분당 60콜**(무료 티어 중 가장 관대). 실시간은 일부 20분 지연/제약, 무료는 주로 US 커버.
- **커버리지**: 실시간 견적, **회사 뉴스/시장 뉴스**, 펀더멘털, 실적 캘린더, 대체데이터 일부.
- **talon 활용**: US 뉴스 피드 + 가벼운 시세/펀더멘털. 무료 뉴스 소스로 유용.

### 2.5 Alpaca (백테스팅 US 분봉의 핵심)
- **성격**: 미국 브로커 + 데이터 API. 무료 Basic 플랜에 히스토리컬 데이터 포함.
- **커버리지**: **10년+ 1분봉(다운로드 수 분 내), 7년+ 히스토리, 200 req/min**. 무료는 IEX 피드, 전체 SIP(CTA/UTP 통합) 피드는 유료. 1분~월봉 타임프레임.
- **품질**: TradeStation/Polygon과 1분봉 종가 상관 1.0(사실상 동일).
- **talon 활용**: **US 분봉 백테스팅 데이터의 정답**. 무료로 대량 확보 가능한 유일에 가까운 경로.

### 2.6 SEC EDGAR
- **URL**: `data.sec.gov` / `www.sec.gov`
- **커버리지**: 전 상장사 공시(10-K/10-Q/8-K 등), **XBRL 재무 팩트(companyfacts/companyconcept), 풀텍스트 검색**.
- **무료/한도**: 완전 무료, **키 불필요**. 단 모든 요청에 `User-Agent`(이름+이메일) 헤더 필수, **초당 10요청** 제한, 429/503 시 지수 백오프 권장.
- **안정성**: 정부 프로덕션급 API, 매우 안정.
- **talon 활용**: US 공시 이벤트/펀더멘털 원천. OpenDART의 미국판.

### 2.7 FRED (세인트루이스 연준)
- **커버리지**: **76.5만+ 경제 시계열**(GDP, CPI, 실업률, 금리, 장단기 스프레드, 유동성 등).
- **무료/한도**: 무료 키, **분당 120요청**. 래퍼 `fredapi`, `pandas-datareader`.
- **talon 활용**: US 매크로 레짐 피처(금리 곡선, 인플레, 리세션 시그널). ECOS의 미국판.

---

## 3. 뉴스 소스 (프로그래매틱 접근 & 한도)

### 3.1 한국 뉴스
| 소스 | 접근 | 무료 한도 | 비고 |
|---|---|---|---|
| **네이버 검색 API(뉴스)** | REST, client_id/secret 헤더 | **일 25,000콜**, 콜당 최대 100건, `start` 최대 1000 → 쿼리당 최대 1,000건 접근 | 제목/요약/링크 메타데이터만 제공(본문 X). 대량 수집은 앱 분할·캐싱 필요 |
| **언론사 RSS**(연합·한경·매경 등) | RSS 파싱 | 무료 | 부분 본문. **재배포/재RSS는 저작권 문제**(내부 분석용에 한정) |
| 다음/카카오 검색 API | (뉴스 검색 지원 축소/종료 추세) | - | 네이버 대비 비권장 |

- **talon 활용**: 네이버 검색 API로 종목/섹터 키워드 뉴스 헤드라인 수집 → LLM(구독제)로 요약·센티먼트. 본문 필요 시 RSS/개별 크롤로 보완하되 내부용에 한정.

### 3.2 미국 뉴스
| 소스 | 무료 한도 | 강점 |
|---|---|---|
| **GDELT** | **키 불필요, 완전 무료**, 15분 갱신, 100개 언어, BigQuery/파일 다운로드 | 글로벌 뉴스·이벤트·엔티티·톤(센티먼트) 대량. NewsAPI 무료 대안 |
| **Finnhub News** | 분당 60콜(무료) | 종목별 company-news / market-news 직접 연동 |
| **Marketaux** | 일 ~100콜(무료) | **기사→티커 매핑 + 센티먼트 스코어**, 80개 마켓/5000+ 소스 |
| **Alpha Vantage News&Sentiment** | 일 25콜(무료, 공유 쿼터) | AI 센티먼트, 200k+ 티커 |
| NewsAPI.org | 일 100콜(개발용), 본문 X, 24시간 지연 | 범용, 프로덕션엔 부적합 |

- **talon 활용**: GDELT(대량·무료 백본) + Finnhub/Marketaux(티커 정밀 매핑·센티먼트) 조합. 원문 심층 요약은 구독제 LLM에 위임.

---

## 4. 백테스팅용 과거 데이터 대량 확보 경로

### 4.1 한국 (KR)
- **일봉**: **pykrx + FDR로 무료 대량 확보 가능**(전종목 수년~수십년). 수급/펀더멘털/상폐 리스트까지 포함 → 팩터 백테스팅에 충분.
- **분봉**: 무료 정공법 부재가 핵심 난점.
  - (a) **토스증권 OpenAPI `/v1/market/candles`**: 분봉~월봉 지원. 다만 과거 룩백 한도가 커버 관건(공식 명세 확인 필요) → 실시간부터 **직접 누적 저장**하는 전략 권장.
  - (b) **KIS(한국투자증권) open-trading-api / 키움 / 크레온**: 분봉 조회 가능하나 룩백 제한(보통 수개월). 장기 분봉은 매일 수집해 자체 DB에 적재하는 방식.
  - (c) **KRX Data Marketplace / 코스콤 유료 데이터셋**: 장기 분/틱을 한 번에 사는 유일한 정공법(비용 발생).
- **권장**: talon은 **일봉 백테스팅부터 무료로 폭넓게** 시작하고, 단타용 분봉은 **토스/브로커 API로 실시간 누적 → 자체 파케이/DB 축적**을 초기부터 가동. 장기 분봉이 꼭 필요하면 그때 유료 검토.

### 4.2 미국 (US)
- **일봉**: FDR / yfinance / **Stooq(무료 벌크 CSV)**로 대량 확보. 상폐 포함 유니버스는 FDR·Nasdaq 리스트로 보강.
- **분봉**: **Alpaca 무료 티어가 정답**(10년+ 1분봉, IEX 피드). 전체 SIP 품질이 필요하면 Alpaca 유료 또는 Polygon 유료.
- **권장**: US는 KR과 달리 무료로 분봉 백테스팅 인프라가 성립 → Alpaca를 US 분봉 주력, FDR/Stooq를 일봉 주력으로.

---

## 5. qlib + 한국 시장 접목 현황

- **microsoft/qlib 공식 지원 시장**: US, CN(중국), HK, Crypto. **공식 한국(KR) 데이터 어댑터/컬렉터는 없음**.
- **커뮤니티 현황**:
  - qlib에 한국 데이터를 붙인 **완성된 공개 프로젝트는 확인되지 않음**. 표준 경로는 **pykrx/FDR로 받은 OHLCV를 qlib의 `dump_bin`(scripts/dump_bin.py)으로 qlib bin 포맷 변환** → 커스텀 `region`/캘린더 구성하는 **DIY 방식**.
  - 한국 주식용 관련 오픈소스는 존재하나 qlib 연동은 아님: `jjlabsio/korea-stock-mcp`(한국 주식 분석 MCP), `PRISM-INSIGHT`(KIS API 기반 멀티에이전트 KR+US) 등 — 참고 아키텍처로만 유용.
- **talon 시사점**: qlib을 쓰려면 **KR 데이터 어댑터를 직접 구축**해야 함(캘린더=한국 거래일, 소스=pykrx/FDR 덤프). 초기부터 무리하게 qlib에 종속되기보다, 먼저 pandas 기반 팩터/백테스트로 검증 후 qlib 도입 여부를 결정하는 편이 리스크가 낮음. US는 qlib 공식 파이프라인(Yahoo 덤프)을 바로 활용 가능.

---

## 6. talon 관점 종합 권고

1. **실거래·실시간 시세는 토스 OpenAPI로 단일화**(KR+US 모두 지원, OAuth2, 분당 수백 콜, 단 WebSocket 미공개 → REST ~1초 폴링). 계좌 개설이 전제.
2. **일봉 백테스팅 인프라(무료)**: KR=pykrx+FDR, US=FDR/Stooq. 상폐 포함으로 생존편향 완화.
3. **분봉 백테스팅**: US=Alpaca 무료로 즉시 확보, KR=토스/브로커 API로 **오늘부터 누적 적재 파이프라인 가동**(가장 시급). 장기 분봉 필요 시 KRX/코스콤 유료.
4. **공시/재무**: KR=OpenDART, US=SEC EDGAR(둘 다 무료 공식).
5. **거시 레짐 피처**: KR=ECOS, US=FRED.
6. **뉴스**: KR=네이버 검색 API + 언론사 RSS, US=GDELT + Finnhub/Marketaux. 요약·센티먼트는 구독제 LLM(Claude Max/Codex)에 위임해 API 종량 과금 회피.
7. **퀀트 스택**: 초기에는 pandas 기반 팩터/백테스트 → 검증 후 qlib은 US 우선 도입, KR은 커스텀 어댑터 자작 필요성을 감안해 신중히.

### 데이터 리스크 등록부
- **스크래핑 취약성**: pykrx/FDR/yfinance/네이버금융 모두 사이트 개편 시 파손 가능 → 소스 이중화 + 정합성 크로스체크(KRX/토스 기준값) + 실패 알림.
- **KR 분봉 공백**: 무료 장기 분봉 부재 → 실시간 누적을 초기에 시작하지 않으면 나중에 되살릴 수 없음(과거는 못 삼).
- **무료 티어 축소 트렌드**: Alpha Vantage(500→25/일)처럼 무료 한도는 계속 줄어드는 추세 → 핵심 파이프라인을 단일 무료 API에 의존시키지 말 것.
- **저작권(뉴스)**: RSS/본문 재배포는 개인·내부 분석 용도로 한정.

---

## 부록: 주요 출처
- pykrx: https://github.com/sharebook-kr/pykrx , https://pypi.org/project/pykrx/
- FinanceDataReader: https://github.com/FinanceData/FinanceDataReader
- KRX Data Marketplace: https://data.krx.co.kr/
- OpenDART: https://opendart.fss.or.kr/ , 래퍼 https://github.com/FinanceData/OpenDartReader
- 한국은행 ECOS: https://ecos.bok.or.kr/api/
- 네이버 검색 API(뉴스): https://developers.naver.com/docs/serviceapi/search/news/news.md
- 토스증권 Open API: https://developers.tossinvest.com/ , https://home.tossinvest.com/ko/open-api
- yfinance: https://github.com/ranaroussi/yfinance
- Alpha Vantage: https://www.alphavantage.co/support/ , https://www.alphavantage.co/premium/
- Polygon.io: https://polygon.io/
- Finnhub: https://finnhub.io/docs/api
- Alpaca: https://alpaca.markets/data , https://docs.alpaca.markets/us/reference/stockbars
- SEC EDGAR: https://tldrfiling.com/blog/sec-edgar-api-rate-limits-best-practices
- FRED: https://fred.stlouisfed.org/docs/api/fred/
- GDELT: https://www.gdeltproject.org/
- Marketaux: https://www.marketaux.com/
- microsoft/qlib: https://github.com/microsoft/qlib
