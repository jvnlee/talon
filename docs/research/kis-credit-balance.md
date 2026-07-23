# KIS 종목별 신용융자·대주 잔고 (credit_balance_1d) — 실측 스펙과 운영

2026-07-23 작성. B그룹 B-6(종목별 신용융자 잔고)의 구현 기록. 원래 KOFIA FreeSIS로 잡았으나 종목축 부재 확정으로 KIS TR로 대체했다. 모든 수치는 라이브 실측(KIS `daily-credit-balance`, 메인 세션).

## 쉬운 말 풀이

- **신용융자(credit loan)**: 증권사 돈을 빌려 주식을 사는 것(레버리지 매수). 잔고가 많으면 반대매매·청산 압력의 연료
- **대주(stock loan short / 貸株)**: 증권사 주식을 빌려 파는 소매 공매도. 개인이 접근 가능한 공매도 성격
- **공여율(給與率, gvrt)**: 그날 거래량 대비 신규 신용 체결 비중(%)
- **잔고비율(rmnd_rate)**: 상장주식수 대비 남은 신용 잔고 비중(%)
- **체결일(deal_date)**: 거래가 성사된 날 = 우리의 `day`
- **결제일(stlm_date)**: 실제 대금·주식이 오가는 날(T+2, 휴장 순연) = `settle_day`

## 1. KOFIA에는 종목별 신용잔고가 없다 (2026-07-14 리서치 정정 확정)

- FreeSIS(금융투자협회 종합통계)의 **전체 통계 트리 152개 노드를 라이브 전수 열거**해 확인: 신용(信用) 계열 화면은 단 2개뿐이고 둘 다 **시장 집계**(전체/유가증권/코스닥)만 제공.
  - `STATSCU0100000070` 신용공여 잔고 추이(신용거래융자·대주·청약자금대출·예탁증권담보융자)
  - `STATSCU0100000080` 신용거래 체결주수 추이
- 종목(단축코드/ISIN) 축을 가진 화면은 **대차거래 상위10종목**(STATSCU0100000180)뿐 — 신용공여에는 종목 축이 아예 없다. 검색폼 파라미터는 `tmpV1`(자료주기 D/M/Q/Y)·`tmpV45`(시작 YYYYMMDD)·`tmpV46`(종료 YYYYMMDD)+`OBJ_NM`뿐, **종목코드 입력 필드 없음**(삼성전자 005930 검증 불가).
- 앵커 엔드포인트는 `POST /meta/getMetaDataList.do`(JSON, `dmSearch`에 `OBJ_NM`+주기+기간)로 확정, 날짜축 1998-01-30~2026-07-22 재현. 단 값 셀(TMPV2~9)은 순수 JSON 재현에서 null(exbuilder 컬럼레이아웃 핸드셰이크 미재현).
- **판정: FreeSIS는 시장 집계만 → feasible=false.** 종목별 신용잔고의 올바른 출처는 KOFIA가 아니라 KIS TR(또는 KRX data.krx.co.kr)이다. **본 저장소는 KIS 경로만 구현한다(KOFIA 코드 없음).**

## 2. KIS `daily-credit-balance` 실측 스펙

- **경로/TR**: `GET /uapi/domestic-stock/v1/quotations/daily-credit-balance`, `tr_id=FHPST04760000`.
- **파라미터**: `FID_COND_MRKT_DIV_CODE=J`, `FID_COND_SCR_DIV_CODE=20476`, `FID_INPUT_ISCD=종목 6자리`, `FID_INPUT_DATE_1=YYYYMMDD 앵커`.
- **축**: 1콜 = **30행**(체결일 `deal_date` 역순). 응답 배열 키 = `output`.

### 앵커 시맨틱 (핵심 함정)

- 앵커 = **"앵커일 시점 공표 가용 최신 deal부터"** 30행 역순. **앵커일 포함이 아니다.** 앵커 20260722·20260723 모두 첫 행 deal=20260720 실측.
- **[함정] 워크백 앵커를 직전 페이지의 마지막 deal로 주면 세션이 누락된다.** anchor=20151117 → first=20151113 → 20151116 갭 발생 실측.
- **올바른 워크백 = 고정 보폭 앵커 −35 달력일.** 창 30영업일 ≈ 42달력일 > 35 → 인접 창이 겹쳐 **갭 0 보장**. 중복 수 행은 upsert(key=symbol)로 멱등 해소. 코드: `WALKBACK_STRIDE_DAYS=35`.

### 관측 지연 T+3 (룩어헤드 규율)

- 최신 deal은 관측 세션 J 기준 **≈ J−3 세션**. 07-23 밤 기준 최신 deal=07-20(결제 stlm=07-22). `shorting_balance_1d`(T+3)와 동일 계약.
- **15:10 종가베팅 판단 시 최신 deal ≈ J−3** — 실시간 신용 압력 신호로 부적합, **3세션 지연 데이터로만** 소비. 오늘 행이 없는 것은 정상(가용분만 적재).

### 가격은 수정, 주수·금액은 절대 원값

- **[중대] 가격 필드(stck_prpr/oprc/hgpr/lwpr)만 조회시점 수정 스케일.** 2018-04-26 prpr=52,140 = 원주가 2,607,000/50. → **가격 레벨 소비 금지, 참고용.**
- **주수 필드는 절대 원값 확정(재작성 없음).** 분할 경계 실측: 005930 잔고 2018-04-26 356,682주 → 05-04 8,359,855주(50:1 실물 전환 점프가 그대로 남음). 신용 잔고 추이·연속성 분석은 주수 필드로만.

### `*_amt` 단위 미확정 — **백테스트 소비 금지**

- **[미확정] `*_amt` 필드의 단위·의미는 만원/천원 어느 해석도 전 구간(2015·2018·2026 교차) 정합 실패.** 원자값 그대로 저장(스키마 Float64), **소비 금지 플래그**. 후속: KOFIA/KRX 공표 시장합계와 교차검증.

### 항등식 (verify 근거)

- **공여율**: `gvrt(%) = new_stcn / acml_vol × 100` 실측 정합(005930 2,771,679/26,804,038=10.34 ≈ gvrt 10.33). 반올림 2자리라 저활동 종목은 0.00 표기 → verify는 관대한 허용오차(0.15)로 정보성 카운트만.
- **잔고비율**: `rmnd_rate(%) = rmnd_stcn / 상장주식수 × 100`(23,300,119/59.7억=0.39). 상장주식수는 이 데이터셋에 없음 → verify에서 재계산 생략(주수·rate 원값 신뢰).

### 이력 절벽 (백필 하한)

- 앵커 20100104 정상(2009-11-17행까지 확인), 앵커 20050103·19990104 = **0행** → 절벽은 2005~2009 사이. **2016~ 백필에는 무관하게 충분.** 백필 기본 시작 `CREDIT_START=2016-01-04`.
- 코스닥도 정상: 196170 최근(rate 2.39%)·2018(잔고 32.2만주).

## 3. 필드 → 컬럼 매핑 (`credit_balance_1d`, 일 파티션, 키=(symbol,))

| KIS 필드 | 컬럼 | 타입 | 비고 |
|---|---|---|---|
| deal_date | `day` | Date | 체결일(파티션) |
| stlm_date | `settle_day` | Date | 결제일 T+2 |
| stck_prpr/oprc/hgpr/lwpr | `close`/`open`/`high`/`low` | Float64 | **조회시점 수정 스케일 — 참고용** |
| prdy_ctrt | `change_pct` | Float64 | 등락률% |
| acml_vol | `volume` | Float64 | 거래량 |
| whol_loan_new/rdmp/rmnd_stcn | `loan_new_qty`/`loan_repay_qty`/`loan_balance_qty` | Float64 | 융자 신규/상환/잔고 **주수(절대)** |
| whol_loan_new/rdmp/rmnd_amt | `loan_new_amt`/`loan_repay_amt`/`loan_balance_amt` | Float64 | 융자 금액 **(원자값·단위 미확정)** |
| whol_loan_rmnd_rate/gvrt | `loan_balance_rate`/`loan_give_rate` | Float64 | 융자 잔고비율%/공여율% |
| whol_stln_new/rdmp/rmnd_stcn | `short_new_qty`/`short_repay_qty`/`short_balance_qty` | Float64 | 대주 신규/상환/잔고 **주수(절대)** |
| whol_stln_new/rdmp/rmnd_amt | `short_new_amt`/`short_repay_amt`/`short_balance_amt` | Float64 | 대주 금액 **(원자값·단위 미확정)** |
| whol_stln_rmnd_rate/gvrt | `short_balance_rate`/`short_give_rate` | Float64 | 대주 잔고비율%/공여율% |
| — | `fetched_at` | Datetime UTC | 수집 스탬프 |

행 없음 = 그날 해당 종목 미수집/미해당. `deal_date` 결측·비리스트 응답은 빈 리스트 정직 반환.

## 4. 소비 계약 (룩어헤드·안전성)

- 15:10 판단 시 **최신 deal ≈ J−3**(T+3 지연). 실시간 신용압력 신호 아님, 지연 신호로만.
- **`*_amt` 백테스트 소비 금지**(단위 미확정 플래그).
- **가격 필드는 참고용**(조회시점 수정 스케일 — 레벨 소비 금지, 주수로만 잔고 분석).
- 주수 필드는 절대 원값 → 분할 경계에서 실물 점프가 남음(비율·증감 분석 시 주의).

## 5. 백필 규모·경로

- **규모**: ~2,800종목 × 종목당 ~104콜(2016~2026, 보폭 35일) ≈ **29만 콜.** 8rps 페이서로 ~10시간. **미실행 — 사용자 결정 대기.**
- **백필**: `talon credit backfill [--start 2016-01-04] [--end 전거래일] [--symbol 부분집합] [--rps]`. 종목당 고정 보폭 −35일 워크백, 페이지 0행이거나 앵커<하한이면 종목 종료. `parallel_fetch` 8workers·실패율 20% 중단. 재개는 `--symbol` 부분집합으로. 하한은 `max(start, 2016-01-04)`로 클램프. 결과는 day별 그룹핑 후 **파티션당 1회 upsert**(멱등).
- **전방 수집**: eod 잡에 `credit` 스텝 부착(`_load_kr_events` 다음). 종목당 1콜(앵커=오늘) → 30세션 창을 day별로 그룹핑해 결손 파티션만 upsert. KIS 키 없으면 `skipped-no-kis-key`, 실패 격리. 신규 launchd 잡 없음. 수동: `talon credit daily`.
- **검증**: `talon credit verify [--start] [--end]` — 오프라인 저장분 검사, `status` not in {ok, empty}이면 exit 1.

## 6. verify 규칙

- **하드(위반 시 status=issues)**:
  - 음수 잔고 0건: `loan_balance_qty < 0` 또는 `short_balance_qty < 0`.
  - `settle_day >= day`(결제일이 체결일보다 앞설 수 없음).
  - 중복 키 0: (day, symbol) 유일.
- **정보성(강등 아님, 비율/카운트만)**:
  - 잔고 연속성 표본(50종목): 인접 세션 `전일잔고 + 신규 − 상환 == 당일잔고` 성립 비율. 분할·대차 리콜 등 정당한 단절이 있어 100% 미만 정상 → **비율만 정직 기록**.
  - 공여율 재계산 표본: `loan_new_qty/volume×100 ≈ loan_give_rate`(허용오차 0.15, 반올림·분모정의 차 흡수). mismatch 카운트만.
  - 커버리지: days/rows/symbols/first_day/last_day.

## 7. 프로브가 미확정으로 남긴 것

- [미확정] `*_amt` 단위(만원/천원 모두 전 구간 정합 실패) — 소비 금지, KOFIA/KRX 시장합계 교차검증 후속.
- [미확정] 이력 절벽 정확 위치(2005~2009 사이). 2016~ 백필엔 무관.
- [참고] 종목별 신용잔고 대체 공개 출처 = KRX data.krx.co.kr(종목별 신용융자/대주 잔고) — 스팟 교차검증 후보.
