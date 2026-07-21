<!-- 생성: 2026-07-18, Opus 4.8 (1M) 리서치 워크플로(조사 3건 + 핵심주장 8건 + 적대검증 8건 종합). 검증에서 refuted/unverifiable 된 주장은 본문에 판정을 명시함. 갱신시각·주기 주장은 근거 문구 인용. -->

# 15:10 종가베팅용 당일 투자자별 수급 — 실시간·잠정 공급원 조사

> 조사일: 2026-07-18(토, 장중 라이브 관찰 불가 — 문서·포털·공식 예제·규정으로만 조사).
> 판단 시점: 매 거래일 **15:10**(종가 부근 매수, 익일 매도). 목표: 그 시점에 최대한 가까운 종목별 당일 수급(외국인/기관/개인 순매수).
> 이미 확보한 것(재조사 대상 아님)은 `docs/research/kis-endpoint-spec.md` §1.2~1.9 참조. 확정치 파이프라인은 `docs/research/krx-investor-flows.md`.

## 용어 풀이(외래어)

- **잠정(가집계) / 확정**: 잠정 = 장중 임시 추정치(저녁에 확정치로 덮임). 확정 = 장 마감 후 거래소 최종 발표값.
- **완전분류(투자자 유형)**: 외국인·기관·개인 등 '누가 샀나'로 나눈 정통 분류. 확정치는 11분류.
- **프록시(proxy)**: 진짜 값을 못 볼 때 대신 쓰는 근사 신호(예: 프로그램매매 = 기관·외국인 흐름의 근사).
- **창구(회원사, member window)**: 주문이 통과한 증권사 창구. '외국계 창구'는 외국계 증권사 창구 통과분으로, '외국인 국적'과 완전히 같지는 않음.
- **슬롯(slot)**: 거래소가 잠정치를 회원사에 배포하는 고정 시각 버킷.
- **차익/비차익**: 차익거래(현·선물 연계 바스켓) / 비차익거래(단순 바스켓). 비차익은 흔히 기관 방향성 프록시로 쓰임.
- **EOD(end of day)**: 장 마감 후 일괄 집계.

---

## 1. 결론 요약

핵심 판별 질문의 답은 **(a)** 다. 종목별 완전분류(외국인+기관) 잠정 수급의 장중 마지막 갱신이 **14:30**인 것은 KIS 특유의 한계가 아니라, **KRX가 상주회원사에 배포하는 '거래소 잠정집계데이터'의 구조적 슬롯 천장이며 전 증권사 공통**이다(다음 갱신은 전부 장 마감 후 확정치: KRX 15:35·18:00, NXT 20:05). 따라서 KIS를 키움·미래에셋 등으로 바꿔도 14:40~15:30(연속매매 마지막 1시간 + 종가 동시호가)의 종목별 투자자 유형 값은 어디에서도(무료·유료 불문) 얻을 수 없다 — 천장이 벤더가 아니라 KRX 원천에 있기 때문이다. 15:10 판단에 실제로 쓸 수 있는 최선의 조합은 **14:30 잠정치(외국인+기관, 이미 KIS로 수집 중) + 14:30 이후를 메우는 실시간 프록시 3종(외국계 창구 순매수 `frgnmem-pchs-trend`, 종목별 프로그램매매 `program-trade-by-stock`, 거래원 `inquire-member`)** 이며, 이 3종도 talon이 이미 무료로 15:10 스윕에 적재 중이다. 남는 완전 사각지대는 **'종목별 기관'의 14:30 이후 실시간 신호** — 프로그램매매는 차익/비차익 분해가 종목 단위에 없어 기관을 분리할 수 없고, 기관 프로그램의 직접 분해는 '시장 전체' 단위에만 존재한다.

> 근거의 정정(적대검증 반영): "서로 독립인 두 증권사(키움·미래에셋)가 동일 슬롯표를 명시"라는 최초 주장은 사실이 아니다. 슬롯표(09:30·10:00·11:30·13:20·**14:30**)와 "거래소 잠정집계데이터 제공시점" 문구를 **명시적으로 공표한 1차 소스는 키움 [0796]/[1051] 단독**이며, KIS 리포 실측(§1.2, 마지막 슬롯=14:30)이 여기에 수렴한다. 미래에셋 [0266] 화면에는 슬롯표도 '14:30'도 '거래소 잠정집계데이터 제공시점' 문구도 없다(그 화면의 유일한 갱신 안내는 "오후 6시경에 거래소의 정식 데이터로 갱신"뿐). 따라서 교차검증 구조는 사실상 **키움(1차) + KIS 실측 수렴(2차)** 이며, 미래에셋은 '장중=추정 / 저녁=확정' 모델의 방증일 뿐 슬롯 천장의 독립 근거가 아니다.

---

## 2. 공급원 매트릭스

검증 상태: **confirmed** = 1차 소스 문구로 확인 / **partially** = 핵심은 확인되나 세부 정정 또는 미검증 축 존재 / **unverifiable** = 1차 소스로 확인 실패.

우선순위(15:10 판단 유용성) 높은 순.

| # | 공급원 | 무엇(종목별/시장·분류·성격) | 장중 갱신(슬롯/주기) | 지연 | 요건(계좌·OS) | 비용 | 검증 |
|---|---|---|---|---|---|---|---|
| 1 | **KRX 거래소 잠정집계** (KIS `investor-trend-estimate` / 키움 `ka10063` / 미래에셋 [0266]가 공유하는 원천) | 종목별 · 외국인+기관 · **잠정** | 5슬롯: 09:30(외국인만)·10:00·11:30·13:20·**14:30**(마지막). 14:30 이후 장중 갱신 없음 | 슬롯 지연 ±10분(송신 시각 편차) | KIS 계좌(보유) 또는 회원사 REST · macOS OK | 무료 | **confirmed** (천장=키움 1차 + KIS 실측; 중간 슬롯은 KIS 11:20 vs 키움 11:30로 상이하나 마지막 14:30 공통) |
| 2 | **KIS `frgnmem-pchs-trend`** (FHKST644400C0, HTS [0433]) | 종목별 · **외국계 창구**(외국인 프록시, 기관 없음) · 실시간 | 실시간 러닝 누적(연속). 14:30~15:10 커버 | 실틱(리포 실측 틱중앙값 ~55초 — 리포 자체 실측만, 공식 문서 근거 없음) | KIS 계좌(보유) · macOS OK · 이미 적재 §1.9 | 무료 | **partially** (엔드포인트·성격 확인; 주기 수치는 리포 실측만 → 월요일 재검증) |
| 3 | **KIS `program-trade-by-stock`** (FHPPG04650101) | 종목별 · 프로그램(기관/외인 혼합 프록시) · 실시간 | 실시간 당일 누적(연속). 14:30 이후·종가까지 갱신 | 실시간 | KIS 계좌(보유) · macOS OK · 이미 적재 §1.7 | 무료 | **confirmed** (종목별엔 차익/비차익 분해 없음 = '전체 합계'만 → 기관 분리 불가) |
| 4 | **KIS `inquire-member`** (FHKST01010600) | 종목별 · 거래원 top5 + 외국계 창구 집계(외국인 프록시) · 실시간 | 실시간 당일 누적 스냅샷 | 실시간 | KIS 계좌(보유) · macOS OK · 이미 적재 §1.6 | 무료 | confirmed (KRX 거래원 공시 기반, 잠정추정 아님) |
| 5 | **KIS 시장 프로그램** (`comp-program-trade-today` FHPPG04600101 / 웹소켓 `index_program_trade` H0UPPGM0 / `investor_program_trade_today` HHPPG046600C1) | **시장/지수** · 프로그램 차익·비차익 분해, 투자자별 분해(HHPPG…는 기관 직접) · 실시간 | 실시간(1분 누적) | 실시간 | KIS 계좌(보유) · macOS OK · comp는 이미 적재 §1.8 | 무료 | **partially** (C5 정정: 비차익 분해는 comp 단독 아님 — index·investor 경로에도 있음. 단 전부 종목코드 입력 불가 = 시장 단위) |
| 6 | 미래에셋 [0253] 투자자 시장별 시간현황 | **시장 전체** · 투자자별 · 잠정 | "10분마다 갱신"(문구) — 화면 폴링 리프레시로 판단 | — | 증권사 웹/HTS · macOS OK | 무료 | **partially** (10분 문구·시장전체 성격 confirmed; 14:30 이후 값 실제 변동 여부 월요일 검증) |
| 7 | KRX Open API (openapi.krx.co.kr) | — · 투자자별 항목 **없음** · (일별매매정보만) | 없음(투자자 데이터 자체 부재) | — | 무료 키 · macOS OK | 무료 | confirmed (카테고리에 투자자별 없음) |
| 8 | data.krx (MDCSTAT022/023/024, pykrx) | 종목별 · 11분류 · **확정** | 2슬롯: 정규장분 **~15:45 잠정** + 시간외 포함 **~18:00 최종** | 15:45 / 18:00 (둘 다 15:10·15:30 이후) | pykrx 로그인 · macOS OK · 이미 확정 백필 사용 | 무료 | **partially** (C6 정정: "18:00뿐"은 부정확 — 15:45/18:00 2슬롯. 단 둘 다 종가베팅 장중 신호 아님) |
| 9 | 넥스트레이드(NXT) 공표자료 | 시장 · 투자자별 **거래비중(주간)**만 | 장중 종목별 투자자 순매수 **미공표** | 주간 사후 | 웹 · macOS OK | 무료 | **partially** (주간 거래비중만·종목별 없음 confirmed; "확정 20:05" 시각은 NXT 페이지에서 문구 미확인) |
| 10 | 코스콤(KOSCOM) 오픈API / CHECK Expert+ | 실시간 시세·프로그램 강력; 종목별 투자자는 **장마감 후**(당일 15:30 이후) | 종목별 투자자 장중 잠정 상품 **없음** | 장마감 후 | **법인 전용**(개인 계약 불가) · 시세라이센스 계약 | 유료(비공개) | **partially** (법인 전용 confirmed; C7 정정: "14:30 천장 슬롯을 판다"는 부정확 — 종목별 투자자는 장중 잠정 상품 자체가 없음) |
| 11 | FnGuide DataGuide | 종목별 풍부하나 EOD·펀더멘털 중심 | 장중 실시간 종목별 수급 아님 | EOD | **법인 전용** · Windows Excel Add-in(설치형) | 유료 720만원/년(2번째~480만원) | confirmed (요금·법인계약 확인; 'macOS 불가'는 Excel 설치형 추론) |
| 12 | 연합인포맥스 단말 | 수급 분석 제공(원천 KRX) | 개인에게 종목별 장중 잠정을 어느 시각까지 파는지 **불명** | 불명 | 기관·전문가 대상 | 유료 고가(비공개) | **unverifiable** (개인 판매·시각 1차 확인 실패 → 벤더 직접 확인 필요) |
| 13 | 씽크풀 / 팍스넷 등 개인 서비스 | 종목별 수급 화면 있으나 KRX 잠정 **재가공** | 원천이 KRX 잠정 → 14:30 천장 그대로 | 원천 동일 | 웹/앱 · macOS OK | 무료(프리미엄 별도) | confirmed (천장 우회 아님, 추가 효용은 UI뿐) |
| 14 | 대신증권 CREON | 종목별 투자자(HTS 동일) | 슬롯 천장 다르다는 근거 없음 | — | **Windows COM/OCX 전용 → macOS 실행 불가** | 무료(계좌) | 블로그 단독(low). 제약상 이 환경 무익 |
| 15 | 삼성증권 POP [0352] 창구분석 추정 | 종목별 외인·기관 · 증권사 **자체 추정**(거래소 피드 아님) | 장중 **2회**(11:30·13:30) → 15:30경 거래소 데이터로 대체 | 14:30 이후 못 메움 | 삼성 계좌 · macOS OK(웹) | 무료 | confirmed (거래소 슬롯보다 오히려 성김 — 14:30 천장 재확인) |
| 16 | 장중 실시간 공매도 | 종목별 공매도량 · 실시간 | **없음** — 당일 정규장분 15:40↑, 당일 전체 18:10↑(EOD) | EOD | KRX 포털/pykrx · macOS OK | 무료(EOD) | partially ('없음이 확인됨'에 가까움; 실시간 종목별 누적 공매도 소매 상품 없음) |
| 17 | 시간외 대량매매(블록딜)·장중 대량체결 | 대량거래 · 장 마감후 협상 / 장중 체결은 투자자 귀속 없음 | 08:00~09:00 및 15:40~18:00 → 15:10 무관 | — | HTS · macOS OK | 무료 | partially (종가베팅 15:10 장중 수급 프록시로 부적합) |
| 18 | **토스증권 오픈API** (openapi.tossinvest.com · `/llms.txt`) | 투자자/수급/프로그램 항목 **전무**(공식 API) — 시세·종목·주문만 | 없음(투자자 데이터 자체 부재) | — | 토스 계좌·사전신청 · **REST** · macOS OK | 무료 | **문서 확인(사후 보강)** (공식 /llms.txt 6카테고리에 투자자·수급·프로그램 없음) |
| 19 | **LS증권 OPEN API** (openapi.ls-sec.co.kr · t1717/t1601) | 종목별 외인+기관 = **t1717 일별·11분류(EOD)** / 시장전체 투자자 장중 = t1601 · **종목별 장중 잠정 REST TR 미발견** | t1717 EOD · t1601 시장전체 장중 · 종목별 장중 슬롯 문서 미확인 | EOD | LS 계좌+xingAPI+OpenAPI 신청 · **REST+WS** · macOS OK | 무료 | **문서 확인**(카테고리·REST·t1717 일별) · **14:30 슬롯 미검증(사후 보강)** |
| 20 | **NH투자증권 나무 QV OpenAPI** | 시세·체결만 · 종목별 투자자/수급 **없음** | 투자자 데이터 자체 부재 | — | HTS ID · **Windows 32bit DLL(wmca.dll) → macOS 불가**(Wine 우회만) | 무료 | **문서 확인(사후 보강)** (공식 페이지: Windows DLL·투자자 미제공; 2023 종료된 건 모의투자뿐) |
| 21 | 기타 개인 오픈API (미래에셋·삼성·KB·카카오페이증권) | 개인용 프로그래매틱 API로 종목별 투자자/수급 **찾지 못함** | — | — | 웹/HTS만(미래에셋 [0253]/[0266]=6행, 삼성 POP [0352]=15행) · macOS 웹 OK | 무료 | 미검증(사후 보강) — 미래에셋 AnyLink 2017 신규중단·나머지 개인 API 미발견(부재확인 아님) |

**상위 5행 요지**: (1) 종목별 외인+기관 잠정의 장중 천장은 **14:30**(KRX 배포 슬롯, 전 증권사 공통, confirmed) — 브로커 전환으로 못 넘음. (2) 14:30 이후 종목별을 메우는 **외국계 창구 실시간**(`frgnmem-pchs-trend`, 외국인 프록시·기관 없음, 이미 적재) — 엔드포인트 confirmed, 주기 수치는 월요일 재검증. (3) 종목별 **프로그램매매 실시간**(기관/외인 혼합, 차익/비차익 분해 없음 → 기관 분리 불가, confirmed, 이미 적재). (4) 종목별 **거래원 top5+외국계 창구 실시간**(confirmed, 이미 적재). (5) **시장 단위** 프로그램 차익/비차익·투자자별 분해(comp/index/investor 경로, 기관을 시장 단위로만 직접 분해, partially) — 종목 단위 불가.

---

## 3. 천장 구조 — 잠정치는 어디서 만들어져 어떻게 배포되나

1. **원천 = KRX(거래소).** 종목별 완전분류 잠정치는 거래소가 장중에 '상주회원사 대상'으로 집계해 배포하는 추정치다. 키움 [0796] 원문: "잠정 데이터는 거래소내 상주회원사 대상으로 집계한 추정치이며 … 거래소 기준자료로 분류되며, 각 증권사에 등록된 외국인 계좌를 통한 장내거래만을 집계", "당일 잠정정보 집계시간 : 9:30(외국인만 제공), 10:00, 11:30, 13:20, **14:30(거래소 잠정집계데이터 제공시점)**". 키움 [1051]: "장중 5차까지 제공되는 거래소 데이터를 제공합니다 … 거래소에서 잠정치 데이터를 수신 받아 당사 서버로 최종자료를 업데이트한 집계시간".

2. **배포 = 회원사(증권사)가 릴레이.** KIS·키움·미래에셋은 이 거래소 피드를 받아 HTS/REST로 재노출할 뿐이다. 그래서 슬롯 천장(마지막 14:30)이 세 브로커 모두 동일하다. KIS 리포 실측(§1.2)도 "오전 max bucket=3, 14:30 부재로 정합", "15:10 잡 시점의 최신 값 = 14:30 슬롯 → 14:30~15:30은 아예 미반영"으로 수렴한다.
   - 슬롯 라벨 미세차: KIS 실측 = 외국인 09:30·11:20·13:20·14:30 / 기관 10:00·11:20·13:20·14:30. 키움 문서 = 09:30(외국인만)·10:00·11:30·13:20·14:30. **중간 슬롯(11:20 vs 11:30)만 다르고 마지막 14:30은 만장일치** — 키움 문서가 "거래소데이터 송신 시간에 따라 다소 차이가 있을 수 있습니다"라 명시한 ±편차 범위.

3. **14:30 이후 = 확정치로 점프.** 다음 데이터는 KRX 확정 15:35(1차)·18:00(정식), NXT 확정 20:05 — 전부 15:10 판단창 밖. 즉 **14:40~15:30(연속매매 마지막 1시간 + 종가 동시호가)은 어떤 장중 잠정 슬롯에도 들어오지 않는다.**
   - 부수 발견: 키움 [0796]이 **KRX 확정을 15:35·18:00 두 시점**으로 명시. talon `krx-investor-flows.md`는 18:00만 기록 중 — 확정을 더 빨리 쓰는 파이프라인이 필요해지면 15:35(1차)를 참고할 것(둘 다 15:10 이후라 종가베팅엔 무용).

4. **유료·타채널도 같은 천장을 공유.** 코스콤(KRX 자회사, 시세 원천)·연합인포맥스·FnGuide 어디에도 '14:30 이후 종목별 외인/기관 분류 슬롯'은 없다. 코스콤·KRX Data의 종목별 투자자 상품은 장중 잠정이 아니라 장마감 후(코스콤 코스닥 종목별투자자 '전일·당일 15:30 이후', KRX MDCSTAT024 잠정 15:45·최종 18:00) 제공이다. 유료가 사주는 것은 실시간 '시세/프로그램/분석'이지 더 늦은 '투자자 분류 슬롯'이 아니다.

**결론**: 천장은 벤더 계층이 아니라 KRX 원천에 박혀 있다. 증권사를 바꾸거나 돈을 써도 넘을 수 없다.

---

## 4. 실시간 프록시 — 진짜 실시간인 것들과 대표성 한계

14:30 천장을 '넘는' 것은 불가능하고, 14:30 이후를 '프록시로 근사'하는 것만 가능하다. 진짜 실시간인 종목별 신호는 다음 셋(전부 KIS·무료·macOS OK·이미 적재)뿐이다.

- **외국계 창구 순매수 — `frgnmem-pchs-trend`(FHKST644400C0, HTS [0433]).** 종목별 외국계 창구 통과 순매수의 실시간 시계열. 15:10 조회 시 대략 13:55~15:10을 커버(리포 §1.9 실측). **한계**: '외국계 창구'≠'외국인 국적'(국내투자자의 외국계창구 이용분 혼입, 외국인의 국내창구 이용분 누락). **기관 데이터 없음.**
  - 정정(적대검증 C4): 이 프록시의 올바른 공식 예제는 `frgnmem_pchs_trend.py`(tr_id=FHKST644400C0)다. 최초 조사가 인용한 `frgnmem_trade_estimate.py`는 **다른 TR**(FHKST644100C0, 랭킹 §1.4)이라 이 메커니즘의 근거가 아니다. 또 "틱 중앙값 ~55초 / 13:55~15:10 커버 / 100틱 롤링"은 **공식 문서 근거가 없고 리포 자체 라이브 실측(§1.9)뿐** — 월요일 재검증 대상.

- **종목별 프로그램매매 — `program-trade-by-stock`(FHPPG04650101).** 프로그램(15종목↑ 동시주문 신고 바스켓) 물량의 실시간 종목별 누적. 기관·외국인 흐름의 프록시. **한계**: 프로그램 물량만 포착(직접주문·비프로그램 매매 누락). **차익/비차익 분해가 종목 단위엔 없다**(실시간 웹소켓 H0STPGM0·체결 FHPPG04650101·일별 FHPPG04650201 모두 '전체 합계'만) → **종목별로 기관을 분리할 수 없다.**

- **거래원 top5 + 외국계 창구 집계 — `inquire-member`(FHKST01010600).** KRX 거래원 공시 기반 실시간 스냅샷(잠정추정 아님). 외국계 창구 순매수는 외국인 프록시로 유효, 프로그램·외국계추이와 상호검증용. **한계**: 상위5 회원사만 + 창구 기준이라 투자자 유형 직접 매핑 불가.

**시장 단위 프로그램 분해(종목별 아님)**: `comp-program-trade-today`(FHPPG04600101), 웹소켓 `index_program_trade`(H0UPPGM0), `investor_program_trade_today`(HHPPG046600C1)는 실시간으로 차익/비차익을 분해하고, 특히 `investor_program_trade_today`는 프로그램을 **투자자(기관 포함)별로 직접 분해**한다. 그러나 셋 다 **종목코드 입력이 없는 시장/지수 단위** — 시장 레짐 게이트엔 쓰되 종목 선정 신호는 아니다. (적대검증 C5: 최초 주장 "비차익 분해는 comp 단독"은 부정확, 그러나 "종목별 기관 실시간 부재" 결론은 불변.)

**완전 사각지대**: **종목별 기관의 14:30 이후 실시간 신호**. 프로그램(종목별)은 차익/비차익을 못 나눠 기관을 못 뽑고, 기관 직접 분해는 시장 단위에만 있다. 무료·유료·증권사 채널 어디에도 종목별 기관의 후천장 경로는 없다.

**프록시 아님이 확인된 것**: 장중 실시간 공매도(EOD 15:40/18:10만), 블록딜(장 마감후), 장중 대량체결(투자자 귀속 없음).

---

## 4.5 토스·LS·기타 증권사 (사후 보강 — 적대검증 미실시)

> 이 절은 최초 워크플로에서 "토스·LS·기타" 담당 조사가 실패해 빠졌던 범위를 뒤늦게 메운 것이다. §1~§4의 적대검증(핵심주장 8건 교차검증) 규율을 거치지 않았으므로 신뢰수준은 그 절들보다 낮다(개별 항목에 근거 표기). 판별 질문은 동일하다 — **"14:30 이후 종목별 외인+기관 수급 갱신을 주는가."**
> **결론: 세 갈래(토스·LS·기타) 어디도 14:30 천장을 넘지 못한다 — 기존 §1 결론과 모순 없음. 오히려 토스·NH는 종목별 투자자 데이터 자체가 없고, LS의 종목별 외인/기관은 일별(EOD)이라 장중 슬롯조차 없다.**

**A. 토스증권 오픈API — 종목별 투자자/수급 항목이 공식 API에 아예 없다(문서 확인).**

- 공식 AI-스펙 `developers.tossinvest.com/llms.txt`가 열거하는 카테고리는 **6종뿐**: Auth · Market Data(호가·시세·체결·상하한가·캔들) · Stock Info · Market Info(환율·휴장일) · Account/Asset · Order. **투자자별 매매동향·수급·외국인/기관 순매수·프로그램매매 엔드포인트는 전무.** 문서 자체가 "The OpenAPI JSON … is always the source of truth for available APIs"라 못박음. 인증은 OAuth2 Client Credentials(서버간).
- 토스 앱의 "투자자 동향(수급)" 화면은 **공식 오픈API가 아니라 토스 웹 내부(비공개) API로 서비스**된다. 비공식 CLI(`tossinvest-cli`)가 이 데이터(개인·외국인·기관 순매수)를 **"웹앱 전용 기능"**으로 분류하고, 공식 API에는 ❌로 표기하며, "토스 웹 내부 API 재사용 … 이용약관(TOS) 위반 소지"라 스스로 경고한다(README). 즉 프로그래매틱 접근 수단은 TOS 위반 스크레이핑뿐.
- 그 내부 데이터조차 토스는 KRX 상주회원사이므로 **원천이 동일한 거래소 잠정집계 → 같은 14:30 천장**(구조적 추론; 토스 문서엔 갱신 시각 문구 없음).
- 판정: **15:10 종목별 수급 목적에 토스 오픈API는 무익**(투자자 데이터 부재). 계좌 개설 이득 0.

**B. LS증권 OPEN API — 종목별 외인/기관은 "일별(EOD)"이고, 종목별 장중 잠정 슬롯 REST TR은 문서로 확인되지 않는다.**

- 요건(문서 확인): 당사 계좌 보유자(개인/법인)에게만 제공, **계좌개설 → xingAPI 사용신청 → OPEN API 사용신청 → 약관동의**, 계좌 단위로 APP Key/Secret 발급(openapi.ls-sec.co.kr/howto-use). LS는 **구 xingAPI(COM, Windows)** 와 **신 OpenAPI(REST+WebSocket)** 를 동시 운영하며, 신 OpenAPI는 REST(총 ~249 REST TR)라 **REST 특성상 macOS 사용 가능**.
- 종목별 외인/기관 = **t1717 「외인기관종목별동향」**. 입력이 `shcode·gubun·fromdt·todt`(기간)이고 출력이 **일자별 1행 + 11분류 완전분류**(사모펀드·증권·보험·투신·은행·종금·기금·기타법인·개인·등록외국인·미등록외국인·국가외·기관·외인계·기타계)다. 즉 **일별(EOD) 확정형 계열**이지 장중 5슬롯 잠정 피드가 아니다(t1717 필드는 XAQueries.py·xing-plus 표준 레퍼런스로 확인).
- 시장 전체 장중 투자자는 t1601/t1615(시간대별 투자자매매추이)로 존재하나 **종목별이 아님** — KIS `comp-program-trade-today`(§2 행5)와 동급 위상.
- 실시간 WebSocket 토픽에 **투자자(외국인/기관) 실시간 유형 없음**(호가·체결·주문만) — 키움/KIS와 동일한 공백(§5-C와 정합).
- **종목별 장중 잠정(거래소 5슬롯) 전용 REST TR은 이번 조사로 찾지 못함(없음 확인은 아님).** 다만 LS도 KRX 상주회원사라, 설령 그런 릴레이가 있어도 **동일 거래소 잠정집계 → 14:30 천장에 묶임**(§3 원천 논거). 정확한 TR 존재·마지막 슬롯 시각은 **월요일 문서 확인 대상**(V7).
- 판정: LS는 종가베팅용 종목별 장중 수급에서 **KIS 대비 추가 이득 없음** — 종목별은 EOD(t1717), 장중은 시장전체(t1601)뿐. 천장과 **정합(모순 없음)**.

**C. 기타 증권사(간단).**

- **NH투자증권 나무 QV OpenAPI**(문서 확인): **Windows 32bit DLL(wmca.dll)·윈도우 이벤트 기반 → macOS 불가**(디스플레이 없는 환경 동작 불가, Wine 우회만). 제공 데이터는 "주식/선물/옵션/ELW 호가·체결"뿐, **종목별 투자자/수급 미제공**. 2023년 종료된 건 모의투자 기능뿐이고 API 본체는 유지(mynamuh.com 공식 페이지). → 목적에 무익.
- **대신증권 CREON**(기존 §2 행14 재확인): CREON Plus는 Windows COM/OCX 전용 → macOS 불가. 종목별 외국인 필드는 있으나(HTS 동일) 원천 KRX·천장 동일. 변동 없음.
- **미래에셋증권**: 개인용 프로그래매틱 오픈API를 **찾지 못함**(레거시 AnyLink는 2017-07-10 신규신청 중단, 이후 개인 REST 개발자센터 부재 — 커뮤니티 근거, 공식 부재확인 아님). 장중 종목별 외인/기관은 **HTS/웹 화면([0253]·[0266])으로만** 접근 = 기존 §2 행6·§3에서 이미 KRX 천장 확인. (참고: "mStock"은 미래에셋 **인도** 법인 서비스로 한국 종목·KRX 수급과 무관 — 범위 밖.)
- **삼성증권**: 개인 오픈API 찾지 못함. 종목별 창구분석 추정은 POP [0352] 웹(기존 §2 행15) — 장중 2회(11:30·13:30) 자체추정 후 15:30경 거래소 데이터로 대체, 천장보다 오히려 성김.
- **KB증권**: 개인 시세/투자자 오픈API 찾지 못함(store.kbsec.com '핀테크스토어'는 앱·알고리즘 마켓플레이스이지 수급 데이터 API가 아님).
- **카카오페이증권**: 개인 오픈API **미제공**(개발자 포럼에 2022·2025 제공 문의만 존재, 제공 근거 없음).

**요지**: 토스·LS·기타 어느 채널도 §1의 14:30 천장 결론을 흔들지 않는다. 토스·NH는 종목별 투자자 데이터 자체가 없고, LS는 종목별=EOD(t1717)·장중=시장전체(t1601)라 종목별 장중 슬롯이 문서상 부재하며, 있어도 KRX 원천 천장에 묶인다. **모순 발견 없음 — 기존 결론 강화.**

**§4.5 출처 URL**

- 토스(공식): https://developers.tossinvest.com/llms.txt · https://developers.tossinvest.com/docs/market-data · https://corp.tossinvest.com/ko/open-api / (비공식·웹앱 스크레이핑) https://github.com/JungHoonGhae/tossinvest-cli
- LS(공식): https://openapi.ls-sec.co.kr/apiservice · https://openapi.ls-sec.co.kr/howto-use · https://openapi.ls-sec.co.kr/about-openapi / (헬퍼·필드 레퍼런스) https://github.com/xorrhks0216/LsApiHelper · https://github.com/skygoldfish/SkyBot/blob/master/XAQueries.py · http://sculove.github.io/xing-plus/xing.html
- NH(공식): https://www.mynamuh.com/WMDoc.action?viewPage=%2FguestGuide%2Ftrading%2FopenAPI.jsp / (래퍼) https://github.com/bekker/qvopenapi-rs
- 기타: 미래에셋 https://securities.miraeasset.com/kairos/0266.htm (행6) · 삼성 http://www.samsungpop.com/contents/poptrading/help/0352.html (행15) · KB https://store.kbsec.com/intro · 카카오페이 https://devtalk.kakao.com/t/api/122181
- (낮은 신뢰·블로그, 방향 참고만) https://algolab.co.kr/blog/kr-broker-api-comparison · https://www.baseload.co.kr/blog/2026-07-07-toss-securities-open-api-guide/

---

## 5. talon 적용 권고

**A. 이미 최적 조합을 확보했다 — 신규 획득물 없음.** 15:10 판단에 쓸 수 있는 최선은 `14:30 잠정(외인+기관) + frgnmem-pchs-trend + program-trade-by-stock + inquire-member`이며, 네 가지 전부 talon 15:10 스윕에 배선되어 있다(§1.2·§1.9·§1.7·§1.6). 이 조사로 추가할 새 무료 원천은 없다.

**B. 15:10 스윕에 추가 검토할 가치가 있는 것**(우선순위):
1. **시장 단위 기관 프로그램 직접 분해 — `investor_program_trade_today`(HHPPG046600C1).** 종목별 기관은 못 얻지만, '시장 전체 기관 프로그램 순매수'를 프록시 없이 직접 받아 **시장 레짐/기관 방향성 게이트**로 쓸 수 있다. KIS·무료·macOS OK. comp(§1.8)가 이미 차익/비차익 시장종합을 주므로 **한계효용은 '투자자별 직접 분해' 부분에 한정** — 추가할지는 게이트 설계 시 판단.
2. 그 외(공매도·블록딜·씽크풀 등)는 15:10 종목 선정에 무익 → **추가 불필요**.

**C. 키움 계좌 개설 — 수급 관점에서 정당화되지 않는다.** 키움 REST `ka10063`은 KIS와 **동일한 KRX 5슬롯(마지막 14:30)** 을 릴레이하고, 실시간 WebSocket 19종에는 투자자 분류(외국인/기관/개인) 실시간 유형이 전무하며 거래원(0F)·프로그램(0w) 프록시만 있어 KIS가 이미 가진 것과 동종이다. 레거시 OpenAPI+(OCX)는 Windows 전용이나 투자자 TR을 REST가 전부 커버해 macOS 손실도 없다. **결론: 수급 확보 목적으로는 키움 개설 이득이 0.** (계좌 개설을 다른 이유 — 체결·수수료·별도 데이터 — 로 검토하는 것은 이 조사 범위 밖.)

**D. 유료 벤더 — 개인·macOS·수급 목적 모두 부적합.** 코스콤(법인 전용), FnGuide(720만원/년·법인·Windows Excel), 연합인포맥스(고가·개인 판매 불명) 어디도 14:30 이후 종목별 투자자 슬롯을 팔지 않는다.

**E. 규율 유지**: 잠정치는 확정과 부호까지 다를 수 있고(장중 추정), 14:30 슬롯은 15:10에 아직 도착 안 했을 수 있으므로 **캡처 벽시계 시각을 반드시 함께 저장**(§3.1 이미 규정). 잠정치를 백테스트에 투입 금지(MEMORY 방침).

---

## 6. 월요일(2026-07-20) 라이브 검증 목록

문서로 확정 못 한 항목만. 오늘 토요일·실계좌 라이브 호출 금지였으므로 첫 15:10 실행에서 확인.

| 항목 | 무엇을 확인 | 방법 |
|---|---|---|
| V1. 14:30 슬롯 실제 도착 시각 | 15:10 호출 시 `investor-trend-estimate` 최신 값이 실제로 14:30 슬롯인지, ±편차로 아직 미도착인지 | 15:10 스윕에서 슬롯/버킷 인덱스 + 캡처 벽시계 시각 비교(±10분 통설 확인) |
| V2. `frgnmem-pchs-trend` 갱신 주기·커버리지 | 틱 중앙값 ~55초·13:55~15:10 커버·100틱 롤링이 실제로 유지되는지(공식 문서 근거 없음, 리포 실측만) | 15:10 응답 100행의 `bsop_hour` 범위·틱 간격 분포 계측(§420/§422/§428 미검증 항목: 15:20+ 동시호가 틱 지속·증감 기준간격·잠정→확정 부호반전 포함) |
| V3. 미래에셋 [0253] '10분 갱신'의 정체 | 14:30 이후에도 시장전체 값이 실제로 변하는지(=거래소 신규 갱신) 아니면 화면 폴링만 도는지 | 14:30·14:40·15:00·15:20 값 스냅샷 비교(정지 시각 페이지 미명시) |
| V4. 키움 `ka10063` 슬롯 필드 | (키움 계좌 개설 시에만) REST 실응답의 슬롯/차수 필드가 문서상 14:30 상한과 일치하는지 | 계좌·앱키 발급 후 15:10 호출 — **사용자 키움 계좌 미보유로 현재 즉시 불가** |
| V5. NXT 확정 20:05 시각 | 'NXT 확정 20:05'의 1차 근거 문구(현재 NXT 미디어자료 페이지에서 미확인, 키움 [0796]엔 있음) | 키움 [0796] 재확인 또는 NXT 규정/공지 직접 확인(정적 페이지라 라이브 불요) |
| V6. 연합인포맥스 개인 판매·시각 | 개인에게 종목별 장중 잠정을 파는지·어느 시각까지인지(현재 unverifiable) | 벤더 영업 직접 문의(라이브 아님) |
| V7. LS OPEN API 종목별 장중 잠정 TR 존재·시각 | LS에 종목별 외인/기관 '장중 5슬롯 잠정' 전용 REST TR이 있는지, 있으면 마지막 슬롯이 14:30인지(현재 t1717=일별만 확인, 장중 종목별 TR 미발견 — §4.5-B) | LS DevCenter [주식]외인/기관·[주식]투자자 TR 목록 정독(정적 문서, 계좌 없이 포털에서 확인 가능·라이브 불요) |

> V5·V6·V7은 라이브가 아니라 문서/벤더 확인 항목(월요일에 함께 처리 편해서 목록에 포함).

### 6.1 검증 결과 【실측 2026-07-20·21】

- **V1 확정 — 14:30 슬롯은 15:10에 항상 도착해 있음.** 이틀 연속 top300 전 종목(300/300)의 `investor_estimate_intraday` 최신 버킷 인덱스 = 5(5슬롯 중 마지막 = 14:30). 미도착 0건. 캡처 벽시계 15:10:35~15:14:31. ±10분 통설보다 안정적.
- **V2 부분 확정 — 100틱 롤링·실시간성은 확인, "중앙값 ~55초·13:55~ 커버"는 보편 아님(체결 밀도 의존).** `frgnmem_trend_intraday` 최신 틱이 이틀 모두 캡처 직전(15:12:20·15:12:23)까지 이어짐 = 14:30 천장 없는 진짜 실시간(§4.3 리포 실측과 정합). 커버 창은 고정 시간이 아니라 활동도의 함수: 활동 많은 종목은 100틱이 마지막 ~30분만(예 000100: 14:40→15:12, 틱 간격 ≈19초), 저활동 종목은 버퍼 미충전 상태로 아침까지 소급(예 000020: 39틱, 마지막 틱이 10:04). 같은 `bsop_hour` 초에 복수 행 존재(동일 초 내 다중 갱신). 적재 종목 279/300(잔여는 외국계 창구 활동 부재로 추정). **15:20+ 동시호가 틱 지속 여부는 여전히 미검증** — 15:10 슬롯 캡처가 15:14 전에 끝나 관측 범위 밖.
- V3(미래에셋 0253 수동 관찰)·V4(키움 계좌 필요)·V5~V7(문서/벤더 확인)은 미실행.

---

## 7. 부록: 전체 URL

**KRX 잠정 슬롯 천장(1차 근거, 키움)**
- https://download.kiwoom.com/hero4_help_new/0796.htm (종목별투자자 — 5슬롯·14:30·확정 15:35/18:00·NXT 20:05 명시)
- https://download.kiwoom.com/hero4_help_new/1051.htm (종목별 투자자별매매추이 잠정 — "장중 5차까지 제공되는 거래소 데이터")
- http://download.kiwoom.com/flash_help_new/1052.htm
- https://download.kiwoom.com/hero4_help_new/1053.htm

**KIS 실시간 프록시(공식 예제)**
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/frgnmem_pchs_trend/frgnmem_pchs_trend.py (올바른 소스: FHKST644400C0, HTS [0433])
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/frgnmem_trade_estimate/frgnmem_trade_estimate.py (별개 TR FHKST644100C0 — C4가 잘못 인용한 소스)
- https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_llm/domestic_stock/program_trade_krx/program_trade_krx.py (H0STPGM0 실시간, 차익/비차익 없음)
- https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/program_trade_by_stock
- https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/program_trade_by_stock_daily
- https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/comp_program_trade_today
- https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_llm/domestic_stock/index_program_trade/index_program_trade.py (H0UPPGM0)
- https://raw.githubusercontent.com/koreainvestment/open-trading-api/main/examples_llm/domestic_stock/investor_program_trade_today/chk_investor_program_trade_today.py (HHPPG046600C1, 투자자별 직접 분해)

**키움 REST/실시간(교차·계좌 검토)**
- https://openapi.kiwoom.com/guide/index?dummyVal=0
- https://raw.githubusercontent.com/younghwan91/kiwoom-rest-api/main/README.md (ka10063 매핑·실시간 19종)
- https://github.com/younghwan91/kiwoom-rest-api
- https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.1.pdf (레거시 OCX = Windows 전용)

**미래에셋(시장전체·프로그램·외국인기관)**
- https://securities.miraeasset.com/kairos/0253.htm ("10분마다 갱신" — 시장전체)
- https://securities.miraeasset.com/kairos/0266.htm (외국인/기관 장중매매현황 추정 — 슬롯표 없음, "오후 6시경 정식데이터 갱신"만)
- https://securities.miraeasset.com/kairos/0272.htm (프로그램 실시간, 코스피200 지수 단위)
- https://securities.miraeasset.com/kairos/0271.htm

**KRX 공식 채널(투자자별 부재/확정)**
- https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd (서비스 목록 — 투자자별 없음)
- https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022&locale=ko_KR
- https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT023&locale=ko_KR
- https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT024&locale=ko_KR (정규장분 15:45 잠정 + 18:00 최종 2슬롯)
- https://short.krx.co.kr/ (장중 실시간 공매도 부재)
- https://data.krx.co.kr/comm/srt/srtLoader/index.cmd?screenId=MDCSTAT300

**NXT(대체거래소)**
- https://www.nextrade.co.kr/menu/mediaData/menuList.do (주간 거래비중만)
- https://www.nextrade.co.kr/

**유료 벤더(개인·수급 목적 부적합)**
- https://koscom.gitbook.io/open-api/how-to-use/procedure/charge (법인 전용)
- https://koscom.gitbook.io/open-api/api/marketv3
- https://koscom.gitbook.io/open-api/api/marketv3/etc/investorsb
- https://koscom.gitbook.io/open-api/api/marketv3/stocks/closeda (종목별 투자자 = 장마감후)
- https://www.koscom.co.kr/portal/main/contents.do?menuNo=200611
- https://www.koscom.co.kr/portal/main/contents.do?menuNo=200612 (CHECK Expert+ = 기관·전문가용)
- https://help-dataguide.fnguide.com/ko/articles/이용-및-요금-안내-48b18a4b (720만원/년, 법인)
- https://dataguide.fnguide.com/
- https://ko.wikipedia.org/wiki/연합인포맥스

**타사·개인 서비스·통설**
- http://www.samsungpop.com/contents/poptrading/help/0352.html (창구분석 추정, 장중 2회)
- https://www.thinkpool.com/ / https://m.thinkpool.com/analysis/supply / https://www.paxnet.co.kr/ (KRX 재가공)
- https://finance.daum.net/domestic/influential_investors
- https://donpoint.co.kr/real-time-foreign-investor-trading-flow/ (대신 CREON, 블로그 단독·low)
- https://algolab.co.kr/blog/kiwoom-rest-api-algotrading-guide-2026 (키움 REST OS독립, 블로그·medium)
- https://namu.wiki/w/블록딜 / https://www.thebell.co.kr/free/content/ArticleView.asp?key=202502271707544440103331

**리포 내부(재조사 대상 아님)**
- `docs/research/kis-endpoint-spec.md` §1.2·§1.4·§1.6·§1.7·§1.8·§1.9·§3.1
- `docs/research/krx-investor-flows.md` (확정치 18:00 파이프라인)
- `docs/research/close-bet-data-stack.md` §4-3
