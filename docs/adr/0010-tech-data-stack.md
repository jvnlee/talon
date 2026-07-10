# 기술·데이터 스택: Python/Polars, Parquet+SQLite, 무료 데이터 소스 이중화

Python 3.12+(uv)과 Polars를 중심으로 하고, 시계열은 Parquet, 운영 상태(포지션·매매 결론·실행 대조·게이트 로그·LLM 사용량)는 SQLite에 저장한다. DB 서버나 qlib `.bin` 배치 스토리지는 1인 감시종목 규모에 과설계라 기각한다. 핵심 타입(매매 결론, 시그널, 게이트 판정)은 pydantic 스키마로 강제한다. 검증 보조로 vectorbt(파라미터 스윕 크로스체크), quantstats(성과 티어시트), exchange_calendars(XKRX 캘린더)를 라이브러리로 채택한다.

데이터 소스: 시세·분봉은 토스 OpenAPI(분봉은 첫날부터 자체 누적), KR 일봉·수급·펀더멘털은 pykrx + FinanceDataReader 이중화(무료 스크래핑 기반 단일 의존 금지, 상호 정합성 크로스체크), 공시는 OpenDART, 거시는 한국은행 ECOS, 뉴스는 네이버 검색 API + RSS(요약·해석은 LLM 레이어). 캔들 차트 이미지는 mplfinance로 렌더링해 브리핑에 첨부한다.
