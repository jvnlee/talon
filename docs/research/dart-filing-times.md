# DART 접수시각 (B-11): 공시 접수시각 시계열

`dart_filings`(opendart `list.json`, 시각 없음)를 보완하는 **접수시각(HH:MM, KST, 24h)** 소급
수집 명세. opendart JSON에는 시각이 없어 별도로 DART 전자공시 웹(`dart.fss.or.kr`)의
`dsac001` 최근공시 목록 화면을 스크레이프해 `dart_filing_times` 데이터셋을 만든다. 수집기는
`src/talon/sources/dart_web.py`(HTML 파서·페이지네이션), `src/talon/ingest/dart_times.py`
(백필·전방·검증), 데이터셋 상수는 `src/talon/data/store.py`. **`dart_filings`·`dart.py`는 불변** —
시각은 신규 데이터셋으로 `rcept_no` 조인만 한다.

프로브 30요청(모두 ≥1s 페이싱, 40캡 미만)으로 축 실측. 아래는 프로브 확정 사실만 기록한다.

## 축 실측 (A / B / C)

세 후보 축을 실측 비교했다:

| 축 | 경로 | 판정 |
|---|---|---|
| **A (채택)** | `dsac001` 최근공시 목록 화면(`mainAll` + `mainO` 탭 조합) | **feasible=true** |
| B (미채택) | opendart 공시상세/개별 접수번호 조회 | 시각 노출 확인되나 종목 순회 = 고비용 |
| C (대조용) | KIND `todaydisclosure.do` | 시각 소급 노출 확인, **거래소채널 한정** — DART 전 우주 대체 불가 |

**축 A는 단일 엔드포인트로는 부족하다.** `dsac001/mainAll.do`("전체" 탭)는 보고서 A(정기)·B(주요사항)·
발행·외부감사·거래소(시장조치)·공정위를 유가/코스닥/코넥스/기타 전 시장으로 덮지만 **지분공시
(filing_type D: 대량보유상황보고 5%룰·임원주요주주 소유상황보고 10%룰)를 제외한다.** 지분공시는
우리 보유 공시의 다수를 차지한다. 지분공시는 같은 `dsac001` 최근공시 일별 목록의 형제 탭
`dsac001/mainO.do`("5%ㆍ임원보고")에 산다 — **동일 테이블·동일 시간 컬럼·동일 `selectDate` 파라미터.**
그래서 **수집기는 하루당 두 탭(mainAll ∪ mainO)을 모두 받고 시간 맵을 `rcept_no`로 전역 키잉한다.**

### KIND 대조 (축 C)

`https://kind.krx.co.kr/disclosure/todaydisclosure.do`(method=`searchTodayDisclosureSub`)는 과거일
(2023-06-01)에도 `시간` 컬럼·HH:MM 셀(18:07, 18:09 등)을 노출해 **접수시각이 두 번째 독립 채널로도
소급 가용함을 확증**했다. 다만 커버리지가 **거래소 소관 공시(거래소/KRX)만**이라 DART 직접제출
정기·주요사항·지분 공시를 빠뜨린다 → 구현축 아님, 교차검증용 통제군일 뿐.

## 엔드포인트·파싱 앵커

- `GET https://dart.fss.or.kr/dsac001/{tab}.do`, `tab ∈ {mainAll, mainO}`.
- 파라미터: `selectDate=YYYYMMDD & currentPage=N & maxResults=100 & mdayCnt=0`.
- **`maxResults`는 서버가 100으로 캡한다**(500 요청해도 100행) → `ceil(totalCnt/100)` 페이지네이션 필수.
- 응답 HTML 앵커(프로브가 JSP 그리드 소스에서 추출한 값만 사용):
  - 총건수: `<input id="totalCnt" value="N">`.
  - 목록: `<div class="tbListInner">` 안 `<table class="tbList">`. `<thead>`에 `<label>시간</label>` 헤더.
  - 각 데이터 `<tr>`: 1번째 `<td>`=시간(HH:MM), 보고서 링크 `href=/dsaf001/main.do?rcpNo=<14자리>`(=`rcept_no`),
    회사 링크 `openCorpInfoNew('<corp_code>', ...)`, 5번째 `<td>`=접수일자 YYYY.MM.DD.
- **fail-loud 규율:** `totalCnt` 또는 시간 헤더 부재 → `SchemaDriftError`. `totalCnt>0`인데 파싱 0행 →
  `SchemaDriftError`(조용한 0행 금지). `totalCnt=0`·파싱 0행 → **진짜 공시 없는 날**로 정상 처리.

## 소급 연한 (horizon)

프로브가 2005-01-03까지 시간 셀 100% 노출을 실측했고 **경계(시간이 사라지는 날)를 찾지 못했다.**
2005/2010과 7일자 매트릭스(2016-07-01 · 2016-08-01 · 2018-01-05 · 2020-06-02 · 2023-06-01 ·
2025-03-04 · 2026-07-22) 전부 HTTP 200 · 시간 셀 100%. **우리 데이터 범위(`dart_filings` 2016-07-01~)
전 구간에서 접수시각은 구조적으로 완전 존재한다.**

- 상수 `DART_WEB_HORIZON = 2005-01-03`. 백필 기본 시작 = `max(2016-07-01, horizon) = 2016-07-01`.
- **소급 연한(2005-01-03) 이전 날짜는 시각 결손이 구조적**이므로 `out_of_horizon`으로 정직 스킵한다.
  우리 범위에서는 실질적으로 발동하지 않는 정합성 가드다(DART 전자공시 인셉션 ~1999-2000이라 더
  이전까지 닿을 개연성 크나 요구 밖이라 미탐색).

## 조인 커버리지

라이브 `dsac001` mainAll+mainO 페이지에서 `rcpNo`(=`rcept_no`)를 뽑아 로컬
`dart_filings/{date}.parquet`의 `rcept_no`와 조인:

| 조인일 | 보유 | mainAll 단독 | mainAll ∪ mainO | 잔여 |
|---|---|---|---|---|
| 2016-08-01 | 117 | 11 (9.4%) | 116 (**99.1%**) | 1 (첨부추가 재발행) |
| 2023-06-01 | 81 | 29 (35.8%) | 79 (**97.5%**) | 2 (교차일 1·첨부추가 1) |

- **결합 커버리지 97.5%·99.1%로 90% 바 통과.** mainAll 단독의 지배적 실패는 지분공시(D) 제외이며
  mainO 추가로 완전 해소.
- 잔여 ~1-2%는 `[첨부추가]`·`[기재정정]` 재발행(신규 `rcept_no`가 dsac001 웹에 독립 행으로 안 뜰 수
  있음 → 시각 없음)과 교차일 그룹핑. **교차일은 시간 맵을 우리 파티션 `day`가 아니라 `rcept_no`로 전역
  키잉해 해소**한다(예: `20230531xxxx`가 opendart에서는 2023-06-01 아래 묶이나 제 접수일 목록
  `selectDate=20230531`에도 등장). 그래서 백필은 **모든 달력일을 순회**해 각 `selectDate` 목록을
  `day` 파티션으로 저장하고, 소비자는 `rcept_no`로 전역 조인한다.

`verify_dart_times`는 표본 연도별 커버리지 %를 **정직 보고(100% 강제 아님)**, 접수시각 형식 유효성
(HH:MM 또는 HH:MM:SS), `rcept_no` 앞 8자리 vs `day`(=교차일 카운트), 파티션 내 중복 키 0을 낸다.

## 요청 예산

- 페이지당 100행 상한. mainAll 평균 4.43p·mainO 평균 1.5p → 하루 평균 ~5.9요청.
- 필터 대상 2016-07-01~2026-07-23 = ~2,475 공시일, 총 **~14,700요청(추정 14k~18k)**. 최근 연도
  증가(2025 표본 687건/일=7p). 1rps ≈ 4.1h, 8rps ≈ 31분. 백필은 **요청 간 1초 페이싱** 고정.
- 최적화: 페이지가 100행 미만이면 그 날 페이징 종료. `has_date` 스킵으로 재개.
- **주의:** dart.fss.or.kr HTML 사이트에는 공식 rate-limit/ToS 안내가 없고 저장소 8rps 페이서는
  OpenAPI/KRX 대상 검증분이다. 다시간 스크레이프는 프로브 30요청과 다른 레짐 → 보수적 1rps 사용.

## PIT 계약 (룩어헤드 규율)

- **`received_time` = DART 표기 접수시각.** 그 시각에 해당 공시가 DART에 공개 접수·존재했다는 **1차
  근거**다. 원천 표기 그대로 저장(HH:MM 문자열, 파생 가공 금지 — VI 시각 문자열 유파와 동일).
- `dart_poll.polled_at`(15:10 폴링 존재 확인 스탬프)과 **의미가 다르다.** `polled_at`은 "우리가
  15:10에 그 공시의 존재를 확인했다"는 관측시각이고, `received_time`은 "DART가 기록한 실제 접수
  시각"이다. 후자가 더 정밀·소급 가능해 과거 구간에도 붙는다.
- **백테스트 소비 용도 = "15:10 이전 접수" 필터 축.** `dart_filings.rcept_no`를
  `dart_filing_times.rcept_no`로 조인해 `received_time`을 얻고 15:10 판단 시점의 T-1/당일 공개
  여부를 결정론적으로 거른다. 시각은 HH:MM(초 없음)·KST 벽시계·24h로, 15:10 게이트에는 충분하나
  거래소 틱 타임스탬프보다 거칠다.
- **소급 연한 밖 구간(2005-01-03 이전)은 시각 결손이 구조적**이다. 다만 우리 데이터 범위
  (2016-07-01~) 전체는 연한 안이라 결손 없음.

## 데이터셋·수집 요약

| 데이터셋 | 파티션 | 키 | 백필 | 전방(eod T-0) |
|---|---|---|---|---|
| `dart_filing_times` | `day`(=`selectDate` 목록일) | `rcept_no` | 달력일 순회, `has_date` 스킵 재개, 1초 페이싱, 연속 3일 실패 중단 | 어제·오늘+최근 7일 자가치유 |

- 컬럼: `day`(Date)·`rcept_no`(String)·`received_time`(String, 원천 표기)·`corp_name`(String)·
  `title`(String)·`source`(String="dart_web")·`fetched_at`(Datetime UTC).
- 전방 수집은 신규 launchd 없이 기존 eod 잡의 `_load_dart_times` 스텝(실패 격리·정직 상태). **DART
  웹은 키·로그인 불필요.**
- **자가치유 구조:** `daily_dart_times`는 오늘 포함 최근 7일을 매 실행 재수집(덮어쓰기)한다. 저녁
  접수분(예: 15:10 이후 접수)이 그날 오후 실행에서 누락되어도 **다음날 실행이 그 날을 재수집해 자연
  보완**한다 — 7일 창이 늦은 정정·추가 접수를 흡수한다.
- CLI: `talon dart-times {backfill,daily,verify}`. `backfill --start` 기본값 =
  `max(2016-07-01, DART_WEB_HORIZON)`.

## 미확인 / 후속

- `dsac001/ExcelDownload.do`가 하루치(시간 포함)를 단일 요청으로 반환하면 ~15k 요청 예산이 급감 —
  전면 백필 전 1프로브 가치. 미검증.
- `[첨부추가]`·`[기재정정]` 잔여의 정확 규모(2조인일에서 ~1-2%): 시대별 스파이크 여부 미측정.
- opendart `list.json`의 `rcept_dt`가 항상 `rcept_no` 14자리 앞부분과 일치하는지(파티션 `day`가
  일부 행에서 접수번호 접두부와 달랐음) — 교차일 전역 키잉으로 우회 중.
- 다시간 ~15k 요청 지속 스크레이프의 봇탐지/세션 스로틀(프로브 30요청은 무이상). mainF(펀드공시)
  포함 필요성은 `stock_code` 필터가 펀드를 이미 떨궈 낮음 — 확인 후속.
