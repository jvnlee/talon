# 프로그램매매 (B-5): KRX 시장단위 + KIS 종목별

프로그램매매(program trading = 지수차익·바스켓 등 사전 등록 알고리즘 주문) 수집 명세. 두 축을 함께
구현한다: **KRX 시장단위 장기 이력**(차익/비차익 분해, 2002~)과 **KIS 종목별 일별**(전체만, 절벽 없음).
수집기는 `src/talon/sources/krx_program.py`(KRX 원문 POST)·`src/talon/sources/kis_market.py::fetch_program_daily`(KIS),
적재는 `src/talon/ingest/program.py`(백필·전방·검증), 데이터셋은 `src/talon/data/store.py`,
배선은 `src/talon/ingest/eod.py`(`_load_program_market`·`_load_program_stock`)·`talon program` CLI.

## 1. KRX 종목별 프로그램매매 부재 확정

**data.krx.co.kr 정보데이터시스템에 종목별(종목-1축) 프로그램매매 화면은 존재하지 않는다.** 프로브가 290개
내비게이션 리프를 전수 열거해 확인:

- 프로그램매매 화면은 **MDCSTAT026 단 하나** (bld `MDCSTAT02601`). 이 화면은 `isuCd`를 받지 않는다 —
  축이 시장(ALL/STK/KSQ)뿐이고 기간 집계만 반환한다.
- 종목 단면 합성 화면(MDCSTAT021)에는 프로그램 bld가 없고, MDCSTAT023은 프로그램이 아니라 투자자별이다.
- 즉 프로그램매매는 **날짜-1 → 전종목 단면도 없고, 종목-1 → 기간 시계열도 없다.** 화면군 전체가 노출하는
  유일한 심볼 축은 시장이다.

따라서 종목별 축은 KIS TR로 대체하고(아래 §3), KRX는 시장단위 장기 이력만 담당한다.

## 2. KRX 시장단위 프로그램매매 — `program_market_1d`

| 항목 | 값 |
|---|---|
| 화면 | `[12012] 프로그램매매 추이` (MDCSTAT026.jsp), 메뉴 통계>기본통계>주식>거래실적>프로그램매매 |
| bld | `dbms/MDC/STAT/standard/MDCSTAT02601` |
| data_key | `output` |
| 축 | **기간 집계** — 1콜(strtDd=endDd=DAY)이 그날 3행 반환(ITM_TP_NM = 차익/비차익/전체) |
| 시장축 | `mktId` ∈ {STK 코스피, KSQ 코스닥} (ALL도 가능하나 저장은 STK·KSQ 분리) |
| 파라미터 | `mktId`, `strtDd`, `endDd`, `share=1`(주), `money=1`(원), `csvxls_isNo=false` |

일별 시장 시리즈 = 날짜 순회(하루당 STK·KSQ 2콜). 인증 세션·재시도·드리프트 가드·`_num` 파서는
`krx_actions._fetch_rows`를 그대로 재사용한다(모든 KRX 화면 공통 `getJsonData.cmd` POST 패턴).

필드 매핑(원문 → 저장, 단위는 share=1·money=1 기준):
`ITM_TP_NM→component`(차익→`arb`/비차익→`nonarb`/전체→`total`),
`ASK_TRDVOL→sell_qty`(매도 수량, 주), `BID_TRDVOL→buy_qty`(매수, 주), `NETBID_TRDVOL→net_qty`(순매수, 주),
`ASK_TRDVAL→sell_value`(매도 대금, 원), `BID_TRDVAL→buy_value`(매수, 원), `NETBID_TRDVAL→net_value`(순매수, 원).

항등식(실측 성립): `net = buy − sell` 수량·대금 모두, `전체 = 차익 + 비차익` 전 필드.
`STK + KSQ = ALL` 가법성 검증(20260722 전체 매도대금: STK 9,372,944,139,086 + KSQ 1,784,943,766,734 =
ALL 11,157,887,905,820, 정확 일치).

이력·공표: 아카이브가 **2002-01-02**까지 닿는다(2001·2000은 0행 = 구조적 부재). 2002~2023 스팟체크 전부 존재.
**당일자 최종 매매내역은 18:00 KST 이후 제공**(화면 각주 "당일자 최종 매매내역은 오후 6시 이후에 제공됩니다").
그래서 전방 수집(`daily_program_market`)은 18:00 게이트를 둔다(그 이전 실행이면 오늘분을 정직하게 미루고
`up-to-date` 반환). 이 이력 심도는 **시장단위 앵커에만** 해당한다 — 종목별은 화면 자체가 없어 이력이 없다.

## 3. KIS 종목별 프로그램매매 — `program_stock_1d`

| 항목 | 값 |
|---|---|
| path | `/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily` |
| TR | `FHPPG04650201` (GET) |
| 파라미터 | `FID_COND_MRKT_DIV_CODE=J`(하드코딩 — UN은 NXT 오염), `FID_INPUT_ISCD`(종목 6자리), `FID_INPUT_DATE_1`(앵커 YYYYMMDD) |
| 축 | 1콜 = 앵커일 포함 **과거 30세션**. 비거래일 앵커는 직전 세션으로 클램프(20260719 일요일 앵커 → 첫 행 20260716) |
| 페이지네이션 | 앵커를 마지막(최고참) 행 직전 세션으로 옮겨 워크백. 30행 미만 = 그 종목 종료(절벽 도달) |

**[중대] 수정 스케일 계약 — 가격·수량은 조회 시점 수정주가·수정거래량(감자·액면분할 소급 반영), 대금만 절대
원.** `kis_minutes §1.12`와 동일한 유파다. 실측: 2016-01-04 005930 `stck_clpr=24,100`(원주가 ~1,205,000,
2018-05 50:1 분할 소급), `acml_vol=15,346,950`(원값 ~30.7만주). 반면 대금 필드(`*_tr_pbmn`)만 당시 실제
규모 — 2016-01-04 `prog_net_val=-140,807,138,000`이 실제 규모. **소비 계약: 가격 레벨·절대 수량 소비 금지,
대금·당일 내 비율만 안전.**

필드 매핑(원문 → 저장): `stck_bsop_date→day`, `stck_clpr→close`(수정), `prdy_ctrt→change_pct`,
`acml_vol→volume`(종목 전체 거래량, 수정 주), `acml_tr_pbmn→value`(종목 전체 거래대금, 절대 원),
`whol_smtn_seln_vol→sell_qty`(수정), `whol_smtn_shnu_vol→buy_qty`(수정), `whol_smtn_ntby_qty→net_qty`(수정),
`whol_smtn_seln_tr_pbmn→sell_value`(절대 원), `whol_smtn_shnu_tr_pbmn→buy_value`(절대 원),
`whol_smtn_ntby_tr_pbmn→net_value`(절대 원). 파생 증감 2필드(`whol_ntby_vol_icdc`·`whol_ntby_tr_pbmn_icdc2`)는
**저장 제외**.

항등식(실측): `net = buy − sell` 수량·대금 모두 성립(671,684 = 5,920,208 − 5,248,524;
182,002,450,750 = 1,593,800,255,500 − 1,411,797,804,750). **차익/비차익 분해 없음(전체만)** — 분해는 KRX
시장단위 축이 담당.

절벽·가용성: **절벽 사실상 없음**(앵커 20050103 정상, 2004-11-22행까지 확인; 2008/2012/2016/2018/2022/
2024/2025 전부 30행 정상). 당일 행은 22:00 KST에 존재 확인, 16:40(eod) 가용성은 미실측 — 30일 창 구조라
`daily`가 가용분만 넣으면 다음날 자동 보완된다. **코스닥 초기 관측: 196170 2016년 부근 프로그램 필드 전부
0**(acml_vol은 정상 — 당시 코스닥 프로그램 미미가 원인 후보, 진위 미확정), 2026-07 앵커는 30행 전부 정상.

## 4. 소비 계약

- **KRX 시장단위(`program_market_1d`)**: 대금·수량·순매수 절대값 안전(비수정 시장 총계). 차익/비차익 분해는
  여기서만. 2002~ 장기 레짐 분석용.
- **KIS 종목별(`program_stock_1d`)**: **대금·당일 내 비율(예: 순매수대금/종목 거래대금)만 소비.** 가격 레벨·
  절대 수량은 조회 시점 수정 스케일이라 시대간·감자 종목 비교 금지. 종목별은 전체만(차익/비차익 없음).
- **교차 대조 금지**: KRX↔KIS 는 수정 스케일(KIS)·집계 기준·시장 vs 종목 합 차이로 불일치가 정상이다.
  `verify_program`은 두 축을 **각각** 자기정합(항등식)으로만 검증하고 상호 대조하지 않는다.
- 15:10 판단에는 항상 T-1 이하만 쓴다(시장단위 18:00 공표, 종목별 당일분은 자가치유 대상).

## 5. 백필 규모 (실행은 사용자 결정)

- **시장단위**: 2002-01-02~현재 ≈ 6,080세션 × 2시장 ≈ **12,160콜** × 0.5s 페이싱 ≈ **1.7시간**. 파티션에
  6행(STK·KSQ × 차익/비차익/전체) 존재 시 스킵(재개), 연속 3세션 실패 중단.
- **종목별**: ~2,800종목 × ~87콜(30세션 창 워크백으로 2005~2026 커버) ≈ **24.3만 콜**. `parallel_fetch`
  8워커, 심볼 단위 워크백(30행 미만이면 종목 종료), 심볼 단위 재개(이미 `start`까지 커버된 종목 스킵).
  **구현만 완료 — 미실행, 사용자 지시 대기.**
- 전방(eod T-0): `daily_program_market`는 18:00 게이트 + 최근 7세션 자가치유. `daily_program_stock`은
  전종목 × 앵커=오늘 1콜, 응답 30세션 창을 day별로 그룹핑해 **파티션당 1회 upsert**(종목×30 반복 upsert
  금지 — read-modify-write 폭주 방지).

## 6. verify 규칙 (`verify_program`, 오프라인)

- **market**: `component` enum(arb/nonarb/total), 중복 키(day·market·component) 0,
  `net = buy − sell` 전 행 항등식, `total = arb + nonarb` 전 필드 항등식, 2002~ 커버리지 요약.
- **stock**: 중복 키(day·symbol) 0, `net = buy − sell` 수량·대금 항등식(값 존재 행만),
  적재 시작 이후 커버리지 요약.
- KRX↔KIS 교차 대조는 하지 않는다(§4 사유).

## 데이터셋·수집 요약

| 축 | 데이터셋 | 키 | 백필 | 전방(eod) | 비고 |
|---|---|---|---|---|---|
| KRX 시장 | `program_market_1d` | (market, component) | 일 순회, 6행 스킵 | 18:00 게이트 + 최근 7세션 | 차익/비차익/전체, 2002~ |
| KIS 종목 | `program_stock_1d` | (symbol,) | 심볼 워크백, 심볼 재개 | 앵커=오늘, day 그룹 1회 upsert | 전체만, 수정 스케일 |

- `talon program {backfill,daily,verify}`. backfill `--part {market,stock}`(기본 market), daily·verify
  `--part`(기본 둘 다). backfill market 기본 시작 2002-01-02.
- eod 스텝은 실패 격리: KRX 로그인 없으면 `skipped-no-krx-login`, KIS 키 없으면 `skipped-no-kis-key`.

## 미확인 / 후속

- KIS 종목별 16:40 eod 시각 당일 행 가용성 미실측(30일 창이 다음날 자동 보완).
- 코스닥 초기(2016 부근) 종목별 프로그램 필드 0의 진위(제도 미미 vs 미제공) 미확정.
- 시장단위 KONEX(KNX) 프로그램 존재 여부 미검증(STK·KSQ만 수집).
