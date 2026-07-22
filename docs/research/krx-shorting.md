# KRX 공매도 (shorting_1d / shorting_balance_1d / shorting_investor_1d) — 실측 스펙과 운영

2026-07-22 작성. B그룹 백필 3번(공매도 일별 데이터)의 구현 기록. 모든 수치는 로그인 경로 라이브 실측(pykrx `_load_pykrx(KrxCredentials)`).

## 쉬운 말 풀이

- **공매도(short selling)**: 주식을 빌려 먼저 판 뒤 나중에 되사서 갚는 거래. 하락에 베팅
- **공매도 거래(trade)**: 그날 체결된 공매도 물량·금액
- **공매도 잔고(balance)**: 아직 되갚지 않고 남아 있는 공매도 포지션(미상환)
- **투자자별(investor)**: 기관/개인/외국인/기타로 나눈 시장 단위 공매도 집계
- **크로스섹션**: 하루치 전 종목 단면
- **연결(consolidated)**: KRX 정규시장 + NXT(대체거래소) 합산 기준

## 1. 세 축과 pykrx 함수

| 데이터셋 | pykrx 함수 | 축 | 단위 |
|---|---|---|---|
| `shorting_1d` (거래) | `get_shorting_volume_by_ticker(date, market)` + `get_shorting_value_by_ticker(date, market)` | 일 × 종목 | 수량(주) / 금액(원) |
| `shorting_balance_1d` (잔고) | `get_shorting_balance_by_ticker(date, market)` | 기준일 × 종목 | 수량(주) / 금액(원) |
| `shorting_investor_1d` (투자자별) | `get_shorting_investor_volume_by_date(from, to, market)` + `get_shorting_investor_value_by_date(from, to, market)` | 일 × 시장 × 투자자 | 수량(주) / 금액(원) |

- **`market="ALL"` 전부 미지원.** 세 함수 모두 ALL은 서버가 `market 옵션이 올바르지 않습니다` 로그 + 빈 DataFrame 반환(예외 아님). → KOSPI/KOSDAQ 시장별 콜 필수. KONEX는 유니버스 밖(잔고/거래 대부분 0)이라 제외.
- 콜당 페이싱 `krx_flows_pause_seconds`(기본 0.5초) 재사용. 거래 4콜/일(시장 2 × vol/val), 잔고 2콜/일, 투자자 4콜/청크.

## 2. 원문 컬럼 → 영문 매핑

### 거래 (get_shorting_*_by_ticker), index=`티커`

| 원문 | volume 콜 → 컬럼 | value 콜 → 컬럼 | 타입/단위 |
|---|---|---|---|
| 공매도 | `short_volume` | `short_value` | Int64 주 / 원 |
| 매수 | `total_volume_consolidated` | `total_value_consolidated` | Int64 — **연결(KRX+NXT) 총거래량/총거래대금** |
| 비중 | `short_ratio_pct` | `short_value_ratio_pct` | Float64 % — **KRX 정본 저장, 재계산 금지** |

- 두 콜 컬럼명이 동일(공매도/매수/비중)해 **호출 함수로 구분**, (day, symbol) 단일 행에 병합.
- `비중` = 공매도 / 매수 × 100 (전 행 산술 일치). `매수`는 "매수측 거래량"이 아니라 **분모=전체 거래량**.

### 잔고 (get_shorting_balance_by_ticker), index=`티커`

| 원문(raw) | 컬럼 | 타입/단위 |
|---|---|---|
| 공매도잔고 / BAL_QTY | `short_balance_qty` | Int64 주 |
| 상장주식수 / LIST_SHRS | `listed_shares` | Int64 주 |
| 공매도금액 / BAL_AMT | `short_balance_value` | Int64 원 |
| 시가총액 / MKTCAP | `market_cap` | Int64 원 |
| 비중 / BAL_RTO | `short_balance_ratio_pct` | Float64 % — **저장 시 qty/listed_shares×100 재계산** |

- **함정**: 원본 `비중`이 `by_ticker`에서 `np.float16`으로 캐스팅되어 유효자릿수 ~3자리 정밀도 손실. 저장 계약: `short_balance_ratio_pct = short_balance_qty / listed_shares × 100`(listed_shares=0이면 0.0). 상장주식수가 응답에 포함되어 자체 검증 가능.

### 투자자별 (get_shorting_investor_*_by_date), index=`날짜`

| 원문 | investor 슬러그 |
|---|---|
| 기관 | institution |
| 개인 | retail |
| 외국인 | foreign |
| 기타 | other |
| 합계 | total |

- volume·value 두 엔드포인트가 **동일 컬럼**(int64). 같은 (day, market, investor) 키로 병합 → `vol_shares`(주), `value_krw`(원).
- `합계` = 기관+개인+외국인+기타 (저장 시 total 행도 보관, verify가 항등식 검증).

## 3. 저장 스키마 (전부 DatePartitionedStore 일 파티션)

- `shorting_1d`: day, symbol, market, short_volume, total_volume_consolidated, short_ratio_pct, short_value, total_value_consolidated, short_value_ratio_pct, fetched_at
- `shorting_balance_1d`: day(=잔고 기준 거래일), symbol, market, short_balance_qty, listed_shares, short_balance_value, market_cap, short_balance_ratio_pct, fetched_at
- `shorting_investor_1d`: day, market, investor, vol_shares, value_krw, fetched_at

행 없음 = 그날 해당 데이터 미수집/미해당. 거래·잔고는 전 유니버스 + 0패딩(잔고 없는 종목도 0행으로 존재).

## 4. 가용성·룩어헤드 규율 (실측 근거 — ADR 0010/0013)

**"언제 알 수 있나"가 최우선.** 세 데이터셋의 eligible 최신일이 서로 다르다.

### 거래 (shorting_1d)
- 공식 공표: 당일 정규장 매매내역 **15:40 이후**, 시간외 포함 전체 **18:10 이후**(KRX 정보데이터시스템 웹 검증). 익일이 아니라 **당일 저녁**.
- `비중`(연결 분모)은 시간외까지 포함하는 18:10 전체공표에서 확정 → 게이트 시각 = **T 18:30**(READY, 18:10 + 여유).
- **함정**: pykrx는 미공표(장중)·휴장일에도 예외 없이 "잘 생긴" 프레임을 돌려줌.
  - 정상 공표일 → 시장 공매도합 > 0
  - 장중 미공표 → 공매도=0 그런데 매수(총거래량 누적)>0
  - 휴장일(2026-07-17) → 공매도=0 **그리고** 매수=0 (+ stderr `None of ['ISU_CD']`)
  - → **준비완료 판정 = 캘린더 거래일 && 시장 공매도합 > 0**. 예외 의존 금지.
- 15:10 종가베팅 판단은 항상 **T-1 확정치**만 소비(당일은 15:40까지 미존재).

### 잔고 (shorting_balance_1d) — 관측 지연 T+3
- `day` = **잔고 기준 거래일(보고의무 발생일 RPT_DUTY_OCCR_DD)**, 조회/관측일이 아님.
- 보고기한 = 기준일 T+2 영업일 18시. 전종목 화면 관측 = **기준일+3 거래세션(T+3) 아침부터**.
  - 실측: 07-16 기준 → 07-21(T+2) 보고 → 07-22 아침 가용. 07-20 기준 → 오늘(07-22) 아침엔 아직 없음.
- 전종목 화면은 trdDd == 기준일 **정확 매칭**(미공표 기준일 조회는 빈 응답, "latest≤trdDd" fallback 아님).
- daily eligible: 실행 세션 J에서 조회 가능한 최신 기준일 = **J−3 세션**. 게이트는 캘린더 세션 산술로만 결정(시각 무관, 항상 3세션 뒤로 유지하므로 룩어헤드 안전). 빈 응답 = 스킵(오류 아님).
- **소비 주의**: 잔고는 항상 T-3로 늦음 → 실시간 숏 압력 신호로 부적합, **3세션 지연 데이터로만** 소비.

### 투자자별 (shorting_investor_1d)
- 거래와 동일 원천(정규장 공매도 체결) → 공표 타이밍 동일, 거래와 같은 READY(18:30) 게이트, 15:10 판단엔 T-1.

## 5. 이력 시작 (백필 하한)

| 데이터셋 | 시작일 | 근거 |
|---|---|---|
| 거래 | **≥ 2010-01-04** 확정 가용 | 실측(2010/2014/2016 조회 모두 실값). 백필 기본 시작 2016-01-01로 잡음 |
| 잔고 | **2016-06-30** | 공매도 잔고 공시제도 시행일. 20160104=빈 응답, 20160630=885행 |
| 투자자별 | **2017-05-22** | 2017 전체년 조회 시 최초 데이터. 2016·2014=1행 0(데이터 없음 시그니처) |

- **투자자별 청커 하드가드**: fromdate < 2017-05-22 이면서 다년 span은 서버가 `'output'` KeyError로 빈 DF 반환 → 청커가 fromdate를 2017-05-22 미만으로 두면 안 됨. 백필은 시작일을 데이터셋별 하한으로 클램프.

## 6. 백필·전방 수집·검증 경로

- **백필**: `talon shorting backfill --dataset {trade|balance|investor|all}` (기본 2016-01-01 ~ 전 거래일). 세션 순회·`has_date` 재개·3연속 실패 자동 중단(계정 보호)·BackfillSummary.
  - 투자자별은 **연 단위 범위 청크 1콜**로 받아 일별로 쪼개 저장(재개 단위는 일별 유지). 청크 전 일자가 이미 있으면 콜 없이 스킵.
- **전방 수집**: eod 잡에 `shorting` 스텝 부착(`_load_investor_flows` 다음). 실패 격리, `steps["shorting"]` 요약. 신규 launchd 잡 없음.
  - 수동: `talon shorting daily`. 데이터셋별 eligible(거래/투자자=today after 18:30, 잔고=J−3)로 직전 7세션 결손 자가치유.
- **검증**: `talon shorting verify` — 오프라인 저장분 검사, `status != ok` 이면 exit 1.

## 7. verify 임계 (프로브 제안 반영)

- **하드(위반 시 status 강등)**:
  - 거래: `short_volume ≤ total_volume_consolidated`(구조상 비중≤100%), 비중 [0, 100+tol].
  - 잔고: `short_balance_qty ≤ listed_shares`, 비중 ≤ 100+tol.
  - 투자자별: `합계 == 기관+개인+외국인+기타`(항등식).
- **소프트(알림만, 강등 아님)**:
  - candle 대비 자리수 알림: `short_volume > candles_1d.volume`. **candles_1d 무거래일 행은 OHLC null·volume 0 계약이므로 null 가드 필수**(조인 미스=null 제외). `공매도 ≤ candle volume`은 KRX-단일 기준이라 고NXT 소형주에서 오탈락 가능 → 하드 아님.
  - 금지기간 새너티: 2020-03-16~2021-05-02·2023-11-06~2025-03-30 창의 시장 공매도합. 전면금지 중에도 시장조성자/LP 잔여 공매도가 **near-zero 아님**(2020-06-15 627,832주·개별 최대 7%, 2024-06-14 2,040,784주·최대 17.4%) → near-zero 하드게이트 금지, `ban_zero_days`(합=0) 카운트만 보고.

## 8. 금지기간이 데이터에 보이는 방식 (실측)

- **투자자별**: 금지 중 개인=외국인=기타=0, 기관만 >0(MM/LP 예외). 합계가 기관 단독값으로 붕괴. 2023-11~2025-03 금지 해제 = 정확히 **2025-03-31**(개인 303,264·외국인 23,664,301 최초 출현).
- **거래 크로스섹션**: §7대로 시장 합계는 1~2자릿수 낮아지나 0 아님.

## 9. 교차 새너티

- 005930 2026-07-21: 공매도 468,511주 ≤ candle volume 20,386,896주 ✓. 단 shorting `매수`(31.09M) ≠ candle volume(20.39M) — NXT 몫 ≈ 34%. **`비중`을 candles_1d로 재구성 금지**.
- 시장 투자자 `합계` = 종목 크로스섹션 공매도 합(2025-06-05 KOSPI 16,266,125주 완전 일치) — verify가 재사용 가능한 항등식.

## 10. 프로브가 미확인으로 남긴 것

- [미확인] 거래 이력 2010-01-04 **이전** 가용성(2010이 확인된 하한).
- [미확인] 투자자별 단일 span(2017-05-22~today) 초장기 범위의 서버 캡 — 3년/연 단위 청크로 회피.
- [미확인] 거래 미공표→공표 사이 15:40~18:10 구간에서 정규장 수치만으로 `공매도`가 최종 확정인지의 분(minute) 단위 경계(18:30 게이트로 안전 마진 확보).
- [미채택] `get_shorting_status_by_date`(종목축 거래+잔고 종합)는 시장 집계에 전종목(~2,800) 루프가 필요해 부적합 — **스팟 교차검증 전용**, 수집기 미구현(프로브 확정).
