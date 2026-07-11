# 팩터 표현식 엔진 — 설계 확정 노트 (2026-07-11)

SYNTHESIS C3의 차용 권고를 구현하며 Opus 4.8 xhigh 리서치 워크플로(qlib 내부 · Alpha census · 안전 파서 · polars 윈도 4갈래 + 종합)로 검증했다. 리서처 2명(qlib 내부, Alpha census)은 구조화 출력 실패했고, 종합 에이전트가 현행 코드 열람 + 로컬 실측으로 공백을 메워 설계를 추인했다. 구현: `src/talon/factors/` (parser/expr/ops/engine).

## 확정 설계

- **표면 문법**: 파이썬 표현식 부분집합. 필드는 베어 네임(`close`, `value`), 연산자는 CamelCase 함수(`Mean(close, 20)`), 인픽스 `+ - * / **`·비교 6종·단항 `-`. `and`/`or`는 Series에 벡터화되지 않으므로 금지 — 시그널 결합은 `If`/`Greater`/`Less`/산술로.
- **파서**: `ast.parse(mode='eval')` → 화이트리스트 `_convert` → frozen dataclass 노드 트리. eval/compile 미호출 (ADR 0005 이행). 가드: 길이 2,000자, 노드 300개, 상수 |x| ≤ 1e12, 거듭제곱은 상수 지수 |exp| ≤ 10, 밑줄 식별자·속성 접근·서브스크립트 등 우회 벡터 20+종 테스트로 고정.
- **컴파일**: 윈도 연산(시계열 `.over("symbol", order_by="day")` / 횡단면 `.over("day")`)마다 중간 컬럼을 물질화하는 **스테이지 분할**. frozen 노드를 키로 공유 부분식 dedup. 요소별 연산은 스테이지 안에서 융합.
- **워밍업 부기** (qlib `get_longest_back_rolling` 상당): Column/Const 0, 요소별 max(자식), 시프트형 자식+n, 윈도형 자식+n−1, 쌍-윈도형 max(자식)+n−1, EMA 자식+4·span, 횡단면 자식. `warmup_periods()`로 정적 산출.
- **null 정책**: rolling 기본 `min_samples=window` 유지 → 워밍업 구간은 명시적 null (qlib은 min_periods=1로 반대지만 룩어헤드 안전성 우선). 윈도 내 null 하나가 윈도 전체를 null화하므로 **결측 처리는 팩터 엔진이 아니라 패널 구성 단계 책임**.
- **연산자 v1 (15종)**: Ref/Delta/Mean/Sum/Std/Max/Min/EMA (시계열) · Abs/Log/Sign/Greater/Less/If (요소별) · CSRank (횡단면). Greater/Less는 qlib 관례대로 요소별 max/min.

## 실측으로 확정된 사실

- **[실측] polars 1.42.1에서 `.over()` 2회 체이닝은 예외 없이 전부 null을 반환한다** (`rolling_mean(2).over("symbol").rank().over("day")` → 조용한 오답). 런타임 에러에 의존할 수 없어 스테이지 분할을 컴파일러가 구조적으로 강제한다. polars 업그레이드 시에도 스테이지 방식은 버전 무관하게 정답.
- **[실측] `ewm_mean(span=N)` 기본값(adjust=True, min_samples=1)은 pandas `ewm(span=N, min_periods=1).mean()` = qlib EMA와 ULP 수준 일치** — 보정 불필요. IIR이라 워밍업은 4·span 규약(가중치 99%+ 수렴).
- **[실측] 전역 `sort("day","symbol")` 사전 정렬만으로 룩어헤드 정답**이나, 리팩터 대비 `.over(order_by="day")`를 방어적으로 병용.
- **[실측] 윈도 내 null 오염**: `[1,None,3,4,5].rolling_mean(3) = [None,None,None,None,4.0]`. 거래정지 결측이 광범위 null을 만들 수 있음 (테스트로 시맨틱 고정).
- **[실측] 룩어헤드 절단 불변**: 레지스트리 전 연산자에 대해 "t일까지 잘라 계산 == 전체 계산의 t일 값"을 파라미터라이즈 테스트로 강제. 새 연산자는 케이스 누락 시 메타 테스트가 실패.

## 미해결 (다음 마일스톤으로)

1. **패널 결측 정책**: 거래정지·신규상장 null의 전방채움 vs 제외 vs min_samples 완화 — 유니버스 필터 설계와 함께 확정.
2. **CSRank 횡단면 범위**: 현재 그날 패널 전체를 랭크. 유니버스 필터가 횡단면을 제한해야 하면 랭크 전 마스크 필요.
3. **Alpha158 이식용 v2 연산자**: Corr/Cov/TSRank/Quantile/WMA — `_pair_window_warmup` 골격은 준비됨. qlib `parse_field`의 정규식+eval은 절대 이식 금지 (qlib은 실제로 eval 사용).
4. **Log/0나눗셈 정의역 위반의 inf/null 전파 정책** — 전략 스코어링 단계에서 방어 규칙 필요.
5. polars rolling_max/min null 엣지 이슈(#23066 등)는 1.42.1에서 미재현 — null 회귀 테스트로 가드해둠.
