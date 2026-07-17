# 종가베팅용 미국 시장 데이터 — 명세와 구현

작성일 2026-07-18 · 리서치: Opus 4.8 xhigh 서브에이전트 10개 workflow(리서치 5 → 종합 1 → 적대 검증 3 → 최종 1) · 구현: 같은 날

## 1. 용어 풀이

- **hold window(보유 창)**: 오늘 종가에 사서 다음 KR 거래일 시가에 파는 사이의 구간. `[오늘 KR 마감, 다음 KR 개장)`. 주말·연휴가 끼면 며칠짜리가 된다.
- **schedule-only(일정 전용)**: "오늘 밤 CPI가 **있다**"만 쓰고 결과값은 안 쓰는 것. 일정은 몇 주 전에 공표되므로 백테스트에 넣어도 룩어헤드가 아니다.
- **staleness(신선도) 검사**: 소스가 최신 세션이 빠진 데이터를 조용히 줘도 잡이 "ok"라고 거짓말하지 않게 기대 세션과 대조하는 것.
- **restatement(소급 재계산)**: Yahoo가 액면분할 때 과거 시세 전체를 다시 계산해 내려주는 것. 일부만 다시 받으면 시계열 스케일이 중간에 끊긴다.
- **mapped session(매핑 세션)**: KR 거래일 D에 대응하는 "D 15:10 이전에 마감한 가장 최근 미국 세션". 요일 계산이 아니라 XNYS 캘린더로 구한다.
- **XNYS**: exchange_calendars 라이브러리의 뉴욕 증시 캘린더 코드.
- **tier(등급)**: 이벤트 심각도. skip(그날 종가베팅 건너뛰기 후보) / shrink(축소 후보) / note(참고).
- **BMO / AMC**: 미국 실적 발표 시점. 개장 전 / 마감 후.

## 2. 핵심 결론 (리서치 종합)

15:10에는 전날 미국 종가가 이미 KR 가격에 반영돼 있다. 그래서 미국 데이터의 실전 가치는 네 곳에 몰린다:

1. **오늘 밤 일정 게이트** — FOMC·CPI·고용 같은 발표와 NVDA·MU 실적이 보유 창 안에 있는가. 전부 KR 폐장 후(21:30 KST~새벽)에 터지므로 15:10엔 "일정"만 알 수 있고, 그거면 충분하다(schedule-only).
2. **익일 아침 매도 입력** — 밤사이 실현된 미국 지수·K-ADR 갭(특히 SKHY·EWY), 실현된 지표 서프라이즈.
3. **KR 오후의 라이브 드리프트** — ES/NQ 선물·USDKRW는 15:10에도 거래 중(이미 pulse가 수집).
4. **국면(regime) 시계열** — ^SOX·VIX·금리 등 수십 년 백필 가능한 일별 계열.

검증 단계에서 기각된 대표 항목: 미국 개별주 1분봉(용도 없음), BTC(인과 방향이 반대), SMH(^SOX와 중복), 국채 입찰 일정(반도체 중심 KOSPI에 전달력 없음), Stooq 헤드리스(PoW 봉쇄 확인), yfinance 실적 날짜(파손 문서화), FRED SP500(라이선스로 10년 제한).

## 3. 채택 항목과 구현 상태

| 항목 | 용도 | 소스(폴백) | 백필 | 저장소 | 잡 | 상태 |
|---|---|---|---|---|---|---|
| 경제지표·연준 일정 (CPI·NFP·PCE·GDP·PPI·claims·소매 + FOMC + ISM 규칙 + 만기·휴장) | 게이트(P0) | FRED release/dates(키 필요) + 연준 페이지 스크랩 + XNYS 유도 | 2016~ 가능 | `us_events`(전방 스냅샷) + `us_events_history` | us-calendar 06:00 매일 | **구현** |
| 벨웨더 실적 캘린더 (NVDA·MU·TSM·TSLA·AVGO·AMD·MRVL·AAPL·SKHY) | 게이트(P0) | Nasdaq 캘린더 JSON(브라우저 UA) + IR 수동 테이블 | IR 수동만 | `us_earnings` | us-calendar | **구현** (IR 테이블은 빈 시드) |
| 한미 매핑 테이블 (effective_from/to 버전 관리) | 조인 키(P0) | 수작성 시드 24행 | 해당 없음 | `us_kr_map` | `talon us-map` 1회 + 분기 점검 | **구현** |
| XNYS 캘린더 헬퍼 (매핑 세션·기대 세션·반일·만기) | 인프라(P0) | exchange_calendars | 로컬 | `markets/us.py` | 공용 | **구현** |
| ES/NQ 15:10 스냅샷 | 라이브(P0) | yfinance (fast_info→history 폴백) | 불가(forward-only) | `macro_intraday` | pulse 15:10/15:35 (기존) + 07:30 추가 | **기존+확장** |
| USDKRW 15:10 | 라이브(P0) | yfinance KRW=X | 부분 | `macro_intraday` | 위와 동일 | **기존+확장** |
| 미국 현물지수·테마 리더 일봉 (^GSPC ^IXIC ^SOX ^DJI ^RUT + 8종 + K-ADR 12종) | 매도·국면(P0/P1) | yfinance (KIS 해외는 엔타이틀 확인 후) | 수십 년 | `us_1d` | us-eod 06:30 화~토 | **구현** (첫 실행 시 2015~ 자동 시드) |
| VIX 종가 | 국면(P1) | CBOE CSV(키리스) → FRED VIXCLS 폴백 | 1990~ | `us_macro_1d` | us-eod | **구현** |
| 미국 금리 2y/10y/2s10s | 국면(P2) | FRED fredgraph CSV(키리스) | 1962~ | `us_macro_1d` | us-eod | **구현** (비용 0이라 동봉) |
| 달러(광의)·USDKRW 일별 | 국면(P2/P1) | FRED DTWEXBGS·DEXKOUS(주간 발행 지연 감안) | 장기 | `us_macro_1d` | us-eod | **구현** |
| USDKRW 온쇼어 종가(ECOS) | 백테스트용 환율 정본(P1) | 한국은행 ECOS(키 필요) | 장기 | 미정 | 미정 | **미구현** — 키 발급 필요 |
| ALFRED 빈티지 지표값 | 서프라이즈 연구(P2) | ALFRED | 가능 | 미정 | 미정 | **보류** — 무료 컨센서스 피드 부재 |

기각 전체 목록과 근거는 workflow 최종 명세(23건) 참조 — 요지는 2절.

## 4. 잡 재설계 (us-night 폐지)

구 us-night(화~토 09:20, yfinance 17종목 일봉 10d+1분봉 5d)의 결함: 07:30 브리핑에 못 쓰는 시각, 신선도 검사 부재(낡은 데이터도 ok), 분할 소급 재계산으로 저장 이력 스케일 단절, 단일 소스, ETF 프록시·지표·캘린더 부재, 백필 부재. → 세 잡으로 대체:

| 잡 | 시각(KST) | 하는 일 | 비고 |
|---|---|---|---|
| `us-eod` | 06:30 화~토 | 일봉(현물지수+리더+K-ADR) + 매크로 계열 전량 갱신. XNYS 기대 세션 대조 — 없으면 **소리 내어 실패**(파셜+텔레그램). 겹침 구간 종가가 0.1% 이상 어긋나면 restatement로 보고 전체 재수신 | 미국 마감(여름 05:00·겨울 06:00) 직후, 07:30 브리핑 전. 1분봉 수집은 폐지(검증 단계 기각) |
| `us-calendar` | 06:00 매일 | 전방 40일 이벤트 + 45일 실적 스냅샷(`in_hold_window` 계산 포함), `--backfill`로 2016~ 이력 1회 적재 | FRED 키 없으면 해당 파트만 skipped-no-key + 경고 |
| `briefing-snapshot` | 07:30 월~금 | USDKRW·ES/NQ 07:30 시세를 `macro_intraday` slot="07:30"으로 | KR 휴장일 스킵. 겨울 07:00~08:00 KST는 CME 정비시간 — 소비 시 참고 |
| `pulse` | 15:10/15:35 | 기존 그대로 (ES/NQ·KRW=X·VKOSPI) | BTC 추가 안 함(기각) |

## 5. 명세와 구현의 의도적 차이

1. **분할 처리**: 명세는 `split_multiplier` 컬럼+읽기 시 조정. 구현은 겹침 구간 불일치 감지 → 전체 재수신·교체. 저장 계열이 항상 단일 스케일이라 하류가 단순해지고 보장은 동등(수익률 불변).
2. **^VIX 심볼 제외**: 명세 체크리스트는 `us_1d`에 ^VIX도 넣었으나 `us_macro_1d`의 VIX(CBOE 공식)와 중복이라 하나로 일원화.
3. **briefing-snapshot 요일**: 명세의 화~토는 오기(토요일 브리핑은 없고 월요일이 빠짐) → 월~금로 수정.
4. **FOMC 의사록·의장 증언·잭슨홀**: v1 제외(신뢰할 기계 소스 부재, note 등급). FOMC 결정일만 수집.
5. **ISM**: 값은 유료라 기각, 일정만 규칙(그 달 1·3번째 XNYS 세션)으로 생성.
6. **실적 IR 테이블**: 빈 시드로 시작 — 과거 발표일을 추정으로 채우는 것은 데이터 조작이라 하지 않았다. `data/usirdates.py`에 IR 확인값을 직접 채우는 구조.

## 6. 사용자 액션 필요

1. **FRED API 키**: https://fred.stlouisfed.org/docs/api/api_key.html 에서 무료 발급 → `TALON_FRED_API_KEY` 설정. 그 후 `talon us-calendar --backfill` 1회(2016~ 이벤트 이력).
2. **launchd 재설치**: `talon launchd install` — us-night plist는 자동 제거되고 us-eod·us-calendar·briefing-snapshot이 걸린다.
3. **`talon us-map`** 1회 실행(매핑 테이블 적재; stock_info와 대조해 모르는 코드를 경고).
4. **첫 `talon us-eod`**: 심볼당 2015~ 전체 시드를 자동 수행(26콜, 수 분). 이후 매일 증분.
5. 환경변수 변경: `TALON_US_NIGHT_SYMBOLS` → `TALON_US_EOD_SYMBOLS` (오버라이드한 적 있으면 갱신).

## 7. 데이터 사용 규율 (룩어헤드)

- `us_1d`·`us_macro_1d`는 KR 날짜 D가 아니라 **mapped session**으로 조인한다. D일 미국 종가는 D일 밤에야 생기므로 KR-날짜 조인은 통째로 룩어헤드다.
- 이벤트 게이트의 진입 피처는 **일정의 존재만** 쓴다. `us_events`에는 결과값 컬럼 자체가 없다(실현 서프라이즈는 익일 매도·해설 전용).
- 15:10 선물·환율 스냅샷과 실적 스냅샷은 forward-only — 백테스트 투입 금지(기존 잠정치 규율과 동일).
- ES/NQ 드리프트의 빼기 기준은 mapped session의 현물 종가(선물-현물 베이시스 섞임 — 정밀값 아니라 톤).

## 8. 남은 오픈 퀘스천 (workflow 최종 명세에서 이관)

- KIS 해외시세 엔타이틀: 기존 국내 앱키로 `HHDFS76240000`(해외 일봉)·`FHKST03030100`(해외지수)·`CTOS5011R`(휴장일)이 되는지 **인증 콜 1회로 확인** 후에만 폴백으로 승격.
- Blue Ocean(한국 낮 미국 개별주) 무료 시세: 현재 무료 청정 소스 없음 — 생기면 15:10 게이트 최대 업그레이드.
- 한미반도체(042700)↔NVDA 연결 강도: 1차 근거 확보 전까지 lead_strength=low 유지.
- ES 드리프트의 과거 복원(Dukascopy CFD)은 베이시스 실측 전 백테스트 금지.
- 이벤트 게이트를 서프라이즈 크기 조건부로 확장할지 — 무료 컨센서스 피드가 없어 보류.
- FRED release/dates가 실키로 미래 일정을 실제 반환하는지 — 키 발급 후 첫 실행에서 확인(문서상 확인됨).
