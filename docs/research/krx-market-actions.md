# KRX 시장조치 이력 (B-4): VI · 시장경보 · 공매도과열 · 거래정지

시장조치 4종의 KRX 데이터포털 화면·bld·필드·축·룩어헤드 규율 실측 명세. 수집기 구현은
`src/talon/sources/krx_actions.py`(원문 POST·파싱), `src/talon/ingest/actions.py`(백필·전방·검증),
데이터셋은 `src/talon/data/store.py`. 모든 화면은 `krx_index.py::fetch_vkospi`와 같은
인증 세션(`pykrx.website.comm.webio.get_session`) 재사용 → `getJsonData.cmd` POST 패턴이다.

공통:

- 엔드포인트: `POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd`
- bld·화면 URL은 아래 표의 값만 상수로 사용(추측 없음, 프로브가 JSP 그리드 소스에서 추출).
- 숫자는 콤마 포함 문자열, 날짜는 `YYYY/MM/DD` 슬래시 → `_num`/`_day` 정규화, 빈값/`-`→null.
- 응답 top키 `output`(SRT 공매도과열만 `OutBlock_1`).
- 저장은 날짜 파티션(`DatePartitionedStore`), **행=이벤트, 빈 파티션=그 날 이벤트 없음(수집 완료 표식)**.

## 1. VI (변동성완화장치 발동) — `vi_events_1d`

| 항목 | 값 |
|---|---|
| 화면 | `[20023] 변동성완화장치 발동종목 현황` (MDCSTAT224.jsp) |
| bld | `dbms/MDC/STAT/issue/MDCSTAT22401` |
| data_key | `output` |
| 축 | **이벤트** — 1행 = (종목 × 발동) 1건. 한 종목 하루 다발동 = 다행 |
| 날짜축 | `strtDd`/`endDd` **범위 질의**(단일일=strt=end), 페이징 없음 |
| 필수 파라미터 | `param1isuCd_finder_stkisu1=ALL`, `isuCd=ALL`, `isuCd2=ALL` (셋 중 하나라도 빈값이면 서버가 조용히 0행 반환 — 계정리스크 함정) |

필드 매핑(원문 → 저장):
`TRD_DD→day`, `ISU_CD→symbol`, `ISU_NM→name`, `MKT_NM→market`,
`VI_KIND_NM→vi_kind`(동적VI→`dynamic`/정적VI→`static`), `VI_TG_TM→trigger_time`(HH:MM:SS),
`VI_RELEAS_TM→release_time`(미해제=빈값→null), `VI_TG_BAS_PRC→reference_price`,
`VI_TG_PRC→trigger_price`, `VI_TG_PRC_DIVRG_RT→divergence_pct`.

**가격 레벨은 발동 당일 비수정(원주가, as-traded).** 정적VI 밴드 ±10.00% 정합·괴리율 삼중항 정합·원
OHLC 동반이 근거. `candles_1d`(수정계수 브리지) 가격과 직접 비교 금지 — 별도 원가 필드로 취급.
(미확인: 액면분할 종목 대 candles 직접 대조는 로컬 candles가 원가+계수≈1 저장이라 미완료. `inqTpCd2=BC`,
`inqTpCd3=Y` 체크박스 의미 미확인 — 기본 `inqTpCd1=01` 단독으로 전건 성립.)

이력 심도·룩어헤드: 아카이브가 제도 인셉션까지 닿음(동적 2014-09-01 / 정적 2015-06-15). 백필 기본
시작은 패널 지평에 맞춰 2016-01-01(`VI_INSTITUTION_START=2014-09-01`은 경계 검증용). **발동/해제
시각은 당일 값**이며 저녁 eod(T-0)에 당기면 그날 전 이벤트의 해제시각이 모두 채워진 완전본이다(진행 중
VI만 미완결인데 마감 후엔 남지 않음). 15:10 판단에는 항상 T-1 이하만 쓴다.

## 2. 시장경보 (투자주의·경고·위험) — `market_alerts_1d`

**두 축이 공존한다:**

| 레벨 | 현황(축A) bld | 지정내역(축B) bld |
|---|---|---|
| 투자주의 caution | `dbms/MDC/STAT/issue/MDCSTAT22801` | MDCSTAT22901 |
| 투자경고 warning | `dbms/MDC/STAT/issue/MDCSTAT23101` | MDCSTAT23201 |
| 투자위험 risk | `dbms/MDC/STAT/issue/MDCSTAT23401` | MDCSTAT23501 |

- **축A 현황(정본, 채택):** 날짜 파라미터 없음 → 호출 시점 지정 중 종목의 스냅샷. 각 행에
  `DESIGN_DD`(지정일)·`RELEASE_DD`(해제일, `-`→null=active) 포함. `mktId=ALL, inqTp=1`(주식).
- **축B 지정내역(미채택):** `isuCd` 필수(공란 0행) → 시장 전체 단면 불가. 최대기간 캡 존재
  (6.5개월 OK, 3년 `INVALIDPERIOD2`). 종목별 순회 = 고비용 on-demand.

**판정: 지정예고 축 부재, 지정 사유 필드 부재**(룰기반 자동지정이라 레벨 자체가 사유). 그래서
`market_alerts_1d`는 **축A 현황을 폴링 거래일 T로 일별 스냅샷 적재**한다:
`day=수집 거래일 T`, `level`←bld(caution/warning/risk), `ISU_CD→symbol`, `ISU_CD_FULL→isin`,
`ISU_NM→name`, `MKT_NM→market`, `DESIGN_DD→design_dd`, `RELEASE_DD→release_dd`.
연속 스냅샷 diff로 지정·해제 이벤트를 복원한다.

룩어헤드: 현황엔 날짜축이 없어 과거 as-of 소급 조회 불가 → **전방 폴링만 정본**. 역사 백필은 축B
종목별(≤2년 창)로만 가능해 backfill에서 forward-only로 보고한다.

> 용어 함정: **투자주의환기종목(환기, `stock_info` SECT_TP_NM) ≠ 투자주의종목(시장경보 1단계)**. 별개
> 신호(전자=코스닥 존속위험 장기 status, 후자=단기 이상급등 룰기반 3단계). 절대 혼동 금지.

## 3. 공매도 과열종목 — `short_overheat_1d`

| 항목 | 값 |
|---|---|
| 화면 | `[34001] 공매도 과열종목` (MDCSTAT309.jsp) |
| bld | `dbms/MDC/STAT/srt/MDCSTAT30901` (전종목/범위) — 30902(개별추이,isuCd필수)·31001(기준표)와 혼동 금지 |
| data_key | `OutBlock_1` |
| 축 | **이벤트** — PK=(`ISU_CD`,`BAS_DD`) |
| 파라미터 | `searchType=1, mktTpCd=0, isuCd="", strtDd, endDd` (과열은 공란 isuCd가 정상 — VI 함정과 다름) |

필드: `BAS_DD→day`(적출일 T-1), `MKTACT_APPL_DD→restrict_apply_dd`(금지적용일 T=익영업일),
`RELEAS_DD→release_dd`, `ISU_CD→symbol`, `ISU_CD_FULL→isin`, `ISU_ABBRV→name`, `MKT_NM→market`,
`MKT_ID→mkt_id`, `VALU_PD_TR_DYS`, `TDD_SRTSELL_WT`, `PRC_YD`(부호 있음), `TDD_SRTSELL_TRDVAL_INCDEC_RT`,
`VALU_PD_AVG_SRTSELL_WT`, `SRTSELL_IMPSBL_DTEC_TP_NM→dtec_type`(유형1/유형2/유형3/유형4/연장 — 2017~2026 백필 전수 실측 5종, 프로브 표본은 유형2·3·연장만 봤으나 실데이터에 유형1 593건·유형4 289건 존재).

룩어헤드(정합): **적출일 BAS_DD=T-1 → 금지 APPL_DD=T.** 전일 종가 후 공표되어 당일 개장 전 확정 →
15:10 T-1 규율과 정합(지표는 BAS_DD 시점값, APPL_DD 이전). 파티션은 BAS_DD 기준.

가용성·백필: 제도 시행 2017-03-27(2016=0행, 2017=179행). 연 청크 범위 조회로 완주. **공매도 전면금지
구간(2020-03-16~2021-05-02, 2023-11-06~2025-03-31)·2017 이전은 구조적 0행 — 에러 아님.** 유형별
채워지는 지표 컬럼이 다르고 임계값이 시대별로 바뀌어(분기 기준표) **지표 절대값의 시대간 비교 불가.**

## 4. 거래정지 — `trading_halts_1d`

권고 경로 = **data.krx.co.kr 두 화면 조합**(KIND `tradinghaltissue.do`는 코드·날짜 부재라 부적합).

| 화면 | bld | 역할 |
|---|---|---|
| MDCSTAT212 매매거래정지종목 현황(스냅샷) | `dbms/MDC/STAT/issue/MDCSTAT21201` | 현재 정지 전 종목 + **사유·정지일·직전매매일** |
| MDCSTAT213 매매거래정지 내역(개별종목) | `dbms/MDC/STAT/issue/MDCSTAT21301` | 정지일·**해제일** 쌍 (isuCd 필수, ≤2년 창) |

- **212 현황(전방 정본):** 날짜축 없음. `mktId=ALL`. 필드 `ISU_CD→symbol`, `ISU_CD_FULL→isin`,
  `MKT_NM→market`, `ISU_NM→name`, `HALT_DESNRELS_DDTM→day`(정지일=이벤트 파티션),
  `HALT_RSN_NM→reason`(**212 전용**), `LST_TRD_DD→last_trade_day`, `resume_day`=null.
- **파티션=정지일(halt_start), key=symbol.** 매 eod에 212 스냅샷을 정지일 파티션으로 upsert.
- **해제일 사후 갱신:** `RESUMP_DD`는 재개 시점에야 채워지는 사후 필드. 스냅샷에서 사라진 종목(=재개)을
  213(`isuCd`=ISIN, `TRD_HALT_DD`/`RESUMP_DD`)으로 재조회해 그 정지일 파티션의 `resume_day`를 채운다
  (`_refresh_resumes`). 여전히 정지 중인 행은 null 유지.

룩어헤드·함정: 212=현재분만·213=개별종목만 — 둘 다 단독 반쪽이라 조합 필수. **사유는 212에만, 해제일은
213에만**(213엔 사유 없음 → 라이브 212를 못 잡은 과거 정지의 사유는 공백, 필요 시 DART 보완). 역사 백필은
유니버스 종목 × ≤2년 창(무거움) → backfill에서 forward-only로 보고. `candles_1d` 무거래(volume=0·OHL
null) 행이 정지일의 내부 교차검증원.

## 데이터셋·수집 요약

| 파트 | 데이터셋 | 백필 | 전방(eod T-0) | 비고 |
|---|---|---|---|---|
| VI | `vi_events_1d` | 월 범위 청크 | `strtDd=endDd=T` 범위+최근창 자가치유 | 원주가 |
| 시장경보 | `market_alerts_1d` | forward-only(현황=스냅샷) | 현황 3레벨 폴링 day=T | 사유·예고 부재 |
| 공매도과열 | `short_overheat_1d` | 연 범위 청크 | 최근창 범위, BAS_DD 분해 | 금지구간 0행 정상 |
| 거래정지 | `trading_halts_1d` | forward-only(212=스냅샷) | 212 스냅샷 + 213 해제갱신 | 사유 212·해제 213 |

- 전방 수집은 신규 launchd 없이 기존 eod 잡의 `_load_market_actions` 스텝(파트별 실패 격리).
- `talon actions {backfill,daily,verify} [--part …]`. backfill 기본 시작 2016-01-01, 기본 파트=vi·overheat.
- `verify_actions`(오프라인): 제도 시작일 이전 데이터 부재, 시각 파싱·유형 라벨 enum, 희소성 자릿수,
  커버리지 요약.

## 미확인 / 후속

- VI 비수정 독립 교차검증(액면분할 종목 대 candles) 미완료 — 데이터 자기정합 3근거로 강하게 지시됨.
- 시장경보 최대기간 캡 정확값(6.5개월<cap<3년, 2년 추정), 지정예고 타 경로 미탐색.
- 공매도과열 최초 지정일 정확값(2017 경계만 확정), KONEX MKT_ID(KNX 추정)·행 상한 미검증.
- 거래정지 213 기간 상한 정확값((2년,10.5년]), 2016 실제 정지행 미포착(2022행으로 메커니즘 확증).
