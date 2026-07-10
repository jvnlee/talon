# 구독제 LLM 기반 자동화 투자 에이전트 하네스 현황 (2025–2026)

> 조사 시점 기준: 2026년 7월. talon 프로젝트(개인 1인용 한/미 주식 단타+스윙 에이전트, 구독제 LLM만 사용, 토스증권 OpenAPI, 텔레그램+macOS) 관점에서 정리.

---

## 0. 핵심 요약 (TL;DR)

1. **구독만으로 자동화는 "현재는" 가능하다.** Claude Max 구독으로 `claude -p`(headless)와 Claude Agent SDK를 API 키 없이 돌릴 수 있고, 이 사용량은 구독 한도에서 차감된다. **단, 이건 2026년에 한 번 뒤집힐 뻔했고 다시 재설계 중인 정책이라 영구 보장이 아니다.**
2. **2026년 청구 정책 대격변(반드시 알아야 함).** 5/13 Anthropic이 "6/15부터 Agent SDK·`claude -p`·GitHub Actions·서드파티 툴은 구독 한도가 아니라 별도 월 크레딧(Max 20x 기준 $200, API 정가 과금)으로 분리"한다고 발표 → 사실상 자동화 워크로드에 종량 과금을 부과하는 변경. **하지만 6/15 시행 당일 Anthropic이 이 변경을 전격 보류(pause)**. 현재는 예전처럼 구독 한도에서 차감되며, 크레딧도 없다. Anthropic은 "재설계 후 사전 공지하겠다"고 함. → **talon은 이 리스크를 아키텍처에 반영해야 한다.**
3. **ToS 핵심 경계선.** Anthropic 소비자 약관은 자동/비인간 접근을 금지하지만 **공식 Claude Code CLI는 명시적 예외**다. 즉 본인이 자기 Max 구독으로 `claude -p`를 cron/launchd로 돌리는 건 허용. 반면 **OAuth 토큰을 빼내 서드파티 클라이언트/커스텀 API 호출로 재사용하는 것은 금지**(2026년 1~2월에 실제 계정 정지 사례·약관 명문화).
4. **Max 20x 워크로드 적합성.** 5시간 롤링 창은 2026/5/6에 영구 2배 확대됐고 Max 20x는 5시간당 대략 200~800+ 프롬프트/~22만 토큰 수준. 주간 상한(전체 1개 + Sonnet 전용 1개)도 존재. **Sonnet 위주로 설계하면 "장중 수 회 + 정기 브리핑" 워크로드는 충분히 들어간다. Opus는 주간 ~40시간급으로 빡빡하니 아껴 써야 한다.**
5. **Codex CLI + ChatGPT Pro는 유사 구조 가능하나 헤드리스 인증이 더 불편.** `codex exec`로 비대화 실행 가능, ChatGPT Pro($200)는 Plus 대비 ~20x 한도. 다만 구독 로그인은 브라우저 OAuth라 헤드리스 서버에서 `auth.json` 복사 트릭이 필요하고, OpenAI는 자동화/CI엔 공식적으로 API 키를 권장한다(=종량). 개인 macOS 상주라면 구독으로 돌릴 수 있으나 회색지대.

---

## 1. Claude Code Headless(`claude -p`) & Agent SDK를 Max 구독으로 쓰기

### 1.1 Headless 모드 (`claude -p`)
- Headless 모드 = 대화형 TUI 없이 프롬프트를 던지면 실행 후 stdout으로 결과를 뱉고 종료. `claude -p "<prompt>"` (= `--print`) 형태.
- cron/launchd/CI에 그대로 꽂아 넣기 좋은 "1회 실행 후 종료" 모델. 파이프 연동도 가능: `tail -f app.log | claude -p 'alert me if you see anomalies'` (Anthropic 공식 예시).
- `--bare` 모드가 스크립트/SDK 호출용 권장 모드이며, 향후 `-p`의 기본값이 될 예정.
- 출력 포맷: `--output-format json`/`stream-json` 등으로 파싱 가능한 구조화 출력을 받을 수 있음(파이프라인에서 필수).

### 1.2 Agent SDK (Python / TypeScript)
- Agent SDK는 Claude Code와 동일한 툴·에이전트 루프·컨텍스트 관리를 프로그램적으로 제공. Python·TypeScript 두 언어 지원.
- **인증 방식이 핵심**: SDK는 raw API 키가 아니라 **Claude Code CLI를 통해 구독 플랜에 인증**한다. 즉 로컬에서 `claude` CLI로 한 번 로그인(구독 계정)해 두면, SDK는 그 세션/자격을 재사용해 Max 구독 한도로 동작한다.
  - 구체적으로는 `claude setup-token`(장기 토큰 생성) 또는 CLI 로그인으로 만들어진 자격을 SDK가 사용. 환경변수 `ANTHROPIC_API_KEY`를 세팅하면 그쪽(종량 API)으로 빠지므로, 구독으로 돌리려면 API 키 환경변수를 비워두고 CLI 인증을 사용해야 한다.
- 주의: 문서상 "Anthropic 인증은 `ANTHROPIC_API_KEY` 또는 `--settings`의 `apiKeyHelper`에서 온다"는 서술도 있는데, 이는 **API 키 경로**를 말하는 것. **구독 경로는 CLI 로그인 세션을 통한다**는 점을 구분해야 한다(둘이 별개 빌링 트랙).

### 1.3 제약 요약
- 구독 경로는 개인용 대화/코딩을 전제로 설계돼 있어, 항상-켜짐(always-on)·대규모 병렬 자동화에는 rate limit이 걸린다.
- 서드파티 앱이 CLI를 안 거치고 OAuth 토큰만 빼서 직접 호출하면 ToS 위반(§8 참조).

---

## 2. 2026년 청구 정책 대격변 — talon 생존에 직결

### 2.1 타임라인
| 시점 | 사건 |
|---|---|
| 2025.08 | 5시간 롤링 창 위에 **주간 상한** 신설 |
| 2026.03 | 2주간 off-peak 한도 임시 2배 |
| 2026.05.06 | Claude Code 5시간 한도 **영구 2배**(Pro/Max/Team/Enterprise), Pro·Max의 피크타임 감축 폐지 |
| 2026.05.13 | **발표**: 6/15부터 Agent SDK·`claude -p`·GitHub Actions·서드파티 툴 사용을 **구독 한도에서 분리 → 별도 월 크레딧(API 정가 과금)** 으로 이전한다고 공지 |
| 2026.06.15 | **시행 당일 전격 보류(pause).** 변경 미적용, 크레딧 없음, 구독 한도에서 계속 차감. "재설계 후 사전 공지" 약속 |

### 2.2 (보류된) 크레딧 안이 어땠나 — 재부활 시 참고
플랜별 월 Agent SDK 크레딧(당시 안, API 리스트 프라이스로 미터링):
- Pro $20 / Max 5x $100 / **Max 20x $200** / Team 표준석 $20·프리미엄석 $100 / Enterprise $20~$200.
- 즉 자동화 워크로드는 "구독 정액"이 아니라 "월 $X 크레딧 소진 후 종량"으로 바뀔 뻔했음. 한 분석은 워크로드에 따라 **실효 단가 12~175배 인상** 효과라고 평가.

### 2.3 talon 시사점
- **현재(2026.07)**: `claude -p`/Agent SDK 자동화가 Max 구독 한도에서 커버됨 → talon의 "구독만 사용" 전제 성립.
- **리스크**: 보류된 것이지 철회가 아님. Anthropic이 재설계해 "자동화 = 크레딧/종량" 모델을 다시 들고 나올 가능성이 상존. 이 경우 talon은 (a) 크레딧 한도 내로 호출 다이어트, (b) Codex 등 대체 수단 병행, (c) 파이프라인 스로틀링으로 대응해야 한다.
- **설계 원칙**: LLM 호출을 하네스에서 추상화(어댑터화)해, 백엔드를 Claude Code↔Codex로 바꿔 끼울 수 있게 만들 것. 호출량(토큰/횟수)을 로깅해 한도/크레딧 소진을 상시 모니터링할 것.

---

## 3. Max 20x 사용량 한도 & talon 워크로드 적합성

### 3.1 현재 한도 구조 (2026)
- **이중 한도**: (1) 5시간 롤링 창(첫 프롬프트 시점부터 5시간) + (2) 7일 롤링 주간 상한.
- Anthropic은 **정확한 프롬프트/토큰 수치를 더 이상 공식 발표하지 않고** 상대 배수(Pro=1x, Max 5x≈5x, Max 20x≈20x)로만 표기.
- 서드파티 추정치(참고용, 공식 아님):
  - 5시간 창: Pro ~44k 토큰(10~45 프롬프트) / Max 5x ~88k(50~200) / **Max 20x ~220k 토큰(200~800+ 프롬프트)**.
  - 주간: 전체 통합 주간 상한 1개 + **Sonnet 전용 주간 상한** 1개. Max 티어는 대략 **주 최대 ~480 Sonnet 시간 또는 ~40 Opus 시간** 수준(동시성·모델 난이도에 따라 변동).
- 2026/5/6 5시간 한도 영구 2배 + 피크 감축 폐지로 실사용 여유가 늘어남.
- 실제 체감은 "메시지 수"가 아니라 **토큰 미터링**(프롬프트+첨부+툴 정의+대화 히스토리 전부 차감)이라는 점 유의. 컨텍스트를 방만하게 쓰면 800 프롬프트도 못 채우고 소진됨.

### 3.2 talon 워크로드 추정
talon 워크로드 = "장중 여러 차례 실행 + 정기 브리핑". 대략적 시나리오:
- 개장 전 브리핑 1회, 장중 스캔/시그널 점검 여러 회(예: 한국장·미국장 각 6~12회), 마감 리뷰 1회, 주말 백테스트/리서치 배치.
- 한 실행이 뉴스 리서치+지표+차트 해석까지 하면 컨텍스트가 커지므로 실행당 수만~십수만 토큰 소비 가능.

**결론**: 
- **Sonnet 중심**으로 라우팅하면 Max 20x의 5시간·주간 한도 안에서 하루 수십 회 실행은 현실적으로 가능. 
- **Opus는 병목**(주간 ~40시간급 추정)이라 심층 리서치/복잡 판단에만 선별 사용. 일상 스캔·요약은 Sonnet, 필요 시에만 Opus 에스컬레이션하는 2단 라우팅 권장.
- 안전장치: 실행 빈도·토큰을 자체 카운팅하고, 한도 근접 시 실행 스킵/지연하는 백프레셔 로직 필요. 하나의 5시간 창에 실행이 몰리지 않게 분산.

---

## 4. cron / launchd 기반 정기 실행 & 상주 에이전트 패턴

### 4.1 Anthropic 내장 스케줄링
- **Routines**: Anthropic 관리 인프라(클라우드)에서 cron 스케줄/‌API 호출/GitHub 이벤트로 무인 실행. 로컬 머신 상시 가동 불필요.
- **세션 내 cron 툴**: `cron_create` / `cron_list` / `cron_delete` — 세션 스코프(대화가 바뀌면 소멸, `--resume`/`--continue`로 복구). 빌드 폴링·PR 감시 등 "세션 살아있는 동안" 용도.
- **`/loop` 스킬**: 프롬프트/슬래시 커맨드를 주기 실행.

### 4.2 OS 레벨 스케줄링 (talon에 가장 적합)
- OS가 타이머를 잡고 헤드리스로 에이전트를 깨우는 방식: **cron, launchd(macOS), Task Scheduler, GitHub Actions**.
- macOS 상주 PC 전제인 talon엔 **launchd `.plist`(StartCalendarInterval)로 장 개장/마감/장중 인터벌마다 `claude -p` 실행**이 정석. cron보다 launchd가 재부팅/로그인 세션 관리에 유리.
- 헤드리스는 "1 프롬프트 실행 후 최종 응답 출력 후 종료"라 crontab/launchd 한 줄에 그대로 들어감.

### 4.3 장시간 상주/메모리 유지 에이전트 패턴
- 전형 패턴: **주기적으로 깨어남 → 상태/메모리 파일 읽음 → 판단·행동 → 메모리 갱신 → 재수면**. talon의 포지션/워치리스트/시그널 상태를 파일(또는 SQLite)로 두고 매 실행 로드/세이브.
- 오픈소스 예: **`T0UGH/agent-cron`** — Claude Agent SDK 태스크를 `.md` 파일로 정의해 cron 스케줄로 실행. talon의 "브리핑/스캔 잡을 마크다운 프롬프트로 관리" 컨셉과 잘 맞음.
- 커뮤니티엔 "15분마다 깨어나 상태 읽고 행동/메모리 갱신 후 재수면"하는 자율 에이전트 사례 다수(무인 스케줄 + 영속 메모리).

---

## 5. Telegram 봇 ↔ Claude Code 연동 오픈소스

### 5.1 공식
- **`anthropics/claude-plugins-official`**의 telegram 플러그인: 봇으로 Telegram에 로그인하는 **MCP 서버**를 띄워, 봇 메시지를 Claude Code 세션으로 포워딩하고 Claude가 능동적으로 메시지 전송 가능.

### 5.2 커뮤니티 (별점·기능 대표)
- **`RichardAtCT/claude-code-telegram`**: 원격에서 Claude Code 접근, **세션 영속성**·프로젝트 관리·권한 제어. 대표격.
- **`linuz90/claude-telegram-bot`**: 텍스트/음성/사진/문서/오디오/비디오 지원, 실시간 툴 사용 가시화. "어디서나 개인 비서".
- **`hanxiao/claudecode-telegram`**: Claude Code용 경량 텔레그램 브리지.
- **`Nickqiaoo/chatcode`**: **폴링 모드**라 공개 IP 없이 아무 PC에서 구동(개인 macOS에 적합).
- **`Angusstone7/claude-code-telegram`**: Claude가 선제적으로 메시지 보내는 telegram MCP 서버 포함, 로컬 설치형.

### 5.3 ToS 관점 선택 기준 (중요)
- **권장**: 봇이 **공식 `claude` CLI를 서브프로세스로 호출**하고 그 출력만 텔레그램으로 중계하는 구조(대다수 위 프로젝트가 이 방식). CLI가 인증을 담당하므로 예외 조항 안에 있음.
- **회피**: 봇이 OAuth 토큰을 추출해 **직접 API를 커스텀 호출**하는 구조 → ToS 위반 소지(§8). "Bring your own API key" 형(예: `grorge123/telegram-claude`)은 종량 API를 쓰는 별개 트랙.
- talon 구현안: 텔레그램 봇 = 입출력 UI, 실제 판단은 launchd/봇 트리거가 `claude -p`(또는 Agent SDK) 서브프로세스를 호출 → 결과를 봇이 전송. 인증·과금은 전부 공식 CLI 경로로 단일화.

---

## 6. 대안: OpenAI Codex CLI + ChatGPT Pro 구독

### 6.1 가능성
- Codex CLI는 **ChatGPT Free/Go/Plus/Pro/Business/Edu/Enterprise에 추가비용 없이 포함**. `codex login`(ChatGPT 로그인) 후 구독 한도로 사용.
- **비대화 실행**: `codex exec "<prompt>"` 로 스크립트/파이프라인 실행. `CODEX_NON_INTERACTIVE=1` 로 무인 설치 등 지원.
- ChatGPT 사용 구분: **Local Messages**(내 머신 실행) + **Cloud Tasks**(OpenAI 인프라 실행), 둘이 5시간 창 공유 + 주간 상한 추가.

### 6.2 한도 (Pro vs Plus)
- Pro는 Plus 대비 **5x 또는 20x** 한도. ($100 티어 ≈ 5x, **$200 티어 ≈ 20x**.)
- 참고 추정치: Pro에서 GPT-5.4 로컬 메시지 **5시간당 400~2,000건**, mini는 1,200~7,000건. Plus는 15~80건 수준. → Pro면 자동화 여지 충분.

### 6.3 자동화 관점의 결정적 차이 (Claude 대비 불리)
- **헤드리스 인증이 번거로움**: 구독 로그인은 "Sign in with ChatGPT" **브라우저 OAuth**라 GUI 없는 서버에서 곤란. 
  - 워크어라운드: 브라우저 있는 신뢰 머신에서 `codex login` → 생성된 **`~/.codex/auth.json`을 헤드리스 머신으로 복사**. Codex가 만료 시 토큰 갱신하며, 갱신된 `auth.json`을 다음 실행 위해 보존. (컨테이너는 **device-code 플로우**도 가능.)
  - talon은 macOS 로컬 상주라 브라우저 로그인 1회로 해결 가능 → 이 마찰은 상대적으로 작음.
- **OpenAI 공식 입장은 자동화=API 키**: 문서가 "CI·SDK·앱 백엔드·헤드리스 잡은 명시적 플랫폼 자격(=API 키, 종량)으로 예산 잡으라"고 안내. 구독으로 `codex exec` 자동화는 "가능하지만 권장 경로가 아님"인 회색지대.

### 6.4 Claude vs Codex 요약
| 항목 | Claude Code(Max 20x) | Codex CLI(ChatGPT Pro) |
|---|---|---|
| 구독 포함 여부 | O ($200 Max 20x) | O ($200 Pro) |
| 비대화 실행 | `claude -p` / Agent SDK | `codex exec` |
| 헤드리스 인증 | CLI 로그인 세션(서버 친화적) | 브라우저 OAuth → `auth.json` 복사 필요 |
| 공식 자동화 태도 | Claude Code CLI는 **명시적 허용** | 자동화는 **API 키 권장**(구독은 회색) |
| 자동화 과금 리스크 | 6/15 크레딧 분리 시도(보류) — 재부활 리스크 | 구독 자동화 대량화 시 API 전환 압박 |
| SDK 생태계 | Agent SDK(Py/TS) 성숙 | CLI 중심, SDK는 API 키 트랙 |

→ **talon 1차 선택은 Claude Code/Max 20x**(공식적으로 자동화 허용 + SDK 성숙), Codex는 백업/헤지 및 정책 급변 시 폴백으로 병행 준비.

---

## 7. 각사 ToS 리스크 정리

### 7.1 Anthropic
- **소비자 약관**: "봇·스크립트 등 자동/비인간 수단"으로 서비스 접근 금지. **예외 = Anthropic API 키 사용 또는 Anthropic이 명시 허용한 경우.**
- **Claude Code는 명시적 예외**: 스크립트·자동화용으로 만든 공식 제품. Pro·Max 구독에서 `claude -p`, 공식 GitHub Actions(크론 CI/CD 포함) 허용. **→ 본인이 자기 구독으로 헤드리스 스케줄 실행하는 talon 용도는 허용 범위.**
- **금지되는 것 (2026 크랙다운)**:
  1. 2026.01 OpenClaw류 **OAuth 토큰 인터셉트→비인가 API 클라이언트 사용** 계정 정지.
  2. 2026.02.19 문서 명문화: Free/Pro/Max의 **OAuth 토큰은 Claude Code·Claude.ai 전용**, 서드파티 툴에서 쓰면 위반.
  3. 계정 공유(개인 사용 전제).
- **결론**: 공식 CLI/SDK를 그대로 쓰면 OK. **토큰을 빼내 커스텀 클라이언트에 넣는 순간 위반.** 프로덕션 상시 자동화의 "정공법"은 Commercial 약관 + API 키(=종량). talon은 개인·단일계정·공식 CLI 경로 유지가 안전선.

### 7.2 OpenAI
- 사용정책: 서비스로 불법·유해 행위 금지, **출력/데이터의 자동·프로그램적 대량 추출 금지.**
- 자동화 이원화: **대화형=ChatGPT 구독 로그인, 자동화/CI/SDK=API 키(플랫폼 종량)** 를 공식 권장. 즉 구독으로 `codex exec` 자동화는 명시 금지는 아니지만 "권장 경로 아님" 회색지대. 헤비 자동화는 API 키로 옮기라는 신호.
- talon 수준(개인 1인, 하루 수십 회, 데이터 대량 스크래핑 아님)은 위반 소지가 낮지만, 규모 확대 시 계정 리스크 존재.

### 7.3 공통 권고
- **단일 개인 계정 + 공식 CLI 경로 고수, 토큰 추출·재사용·계정 공유 금지.**
- 정책 변동(특히 Anthropic 6/15 재설계) 사전 공지를 모니터링하고, LLM 백엔드를 교체 가능하게 추상화.
- 실전/실거래로 확장해 "상시·업무성 자동화"에 가까워질수록, 약관상 정공법은 **Commercial + API 키(종량)** 임을 인지 — 단 이는 프로젝트의 "구독만 사용" 제약과 충돌하므로, 그 시점엔 비용·약관 재평가가 필요.

---

## 8. talon 적용 권고 (실행 지침)

1. **LLM 실행 백엔드**: 1차 Claude Code / Max 20x. `claude -p --output-format json`(또는 Agent SDK Python)로 호출. `ANTHROPIC_API_KEY` 비워 구독 경로 강제. 인증·과금을 공식 CLI로 단일화.
2. **스케줄링**: macOS **launchd .plist**로 한국장/미국장 개장·마감·장중 인터벌 잡 정의. 각 잡은 마크다운 프롬프트(`agent-cron` 스타일) 로드. 상태/포지션/워치리스트는 파일·SQLite로 영속화(깨어남→로드→행동→세이브).
3. **모델 라우팅**: 일상 스캔·뉴스 요약·지표 = Sonnet. 심층 리서치·복잡 판단만 Opus 에스컬레이션. 실행당·주간 토큰을 자체 카운팅해 한도 백프레셔.
4. **텔레그램**: 봇은 입출력 중계 UI. 판단은 launchd 잡 또는 봇 트리거가 공식 CLI 서브프로세스를 호출→결과 전송. **토큰 추출형 커스텀 클라이언트 금지.**
5. **정책 헤지**: (a) LLM 어댑터로 Claude↔Codex 스왑 가능하게 설계, (b) Anthropic 6/15 재설계 공지 모니터링, (c) 크레딧/종량 부활 시 호출량 다이어트·폴백 경로 준비.
6. **ToS 안전선**: 단일 개인 계정, 공식 CLI만, 토큰 재사용·계정 공유 금지, 대량 스크래핑 지양.

---

## 부록: 미해결/추적 필요 항목
- Anthropic이 6/15 "재설계"안을 언제 어떤 형태로 재발표하는지(자동화 크레딧/종량 부활 여부) — talon 경제성 직결.
- Max 20x의 정확한 5시간·주간 수치는 비공개 → 실측 로깅으로 talon 자체 캘리브레이션 필요.
- Agent SDK를 구독으로 인증하는 정확한 최신 절차(`setup-token` vs 로그인 세션)와, 헤드리스 macOS launchd 환경에서의 토큰 갱신 지속성 실검증 필요.
- Codex 구독 자동화의 계정 리스크가 실제로 어느 규모에서 문제되는지(사례 부족).

---

## 출처
- Claude Code Headless 문서: https://code.claude.com/docs/en/headless
- Agent SDK 개요: https://code.claude.com/docs/en/agent-sdk/overview
- Agent SDK를 Claude 플랜으로 사용(공식, 변경 보류 안내 포함): https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- 청구 변경 보류 보도(The New Stack): https://thenewstack.io/anthropic-pauses-claude-agent-sdk-subscription-change/
- 청구 변경 보류 분석(digitalapplied): https://www.digitalapplied.com/blog/anthropic-claude-credit-overhaul-june-15-2026
- 변경 정본 정리(gist, 실효 단가 인상 분석): https://gist.github.com/MagnaCapax/d9177e35b355853f03c730dfcaa693ef
- the-decoder(가격 전쟁 맥락): https://the-decoder.com/anthropic-backs-off-unpopular-billing-overhaul-as-price-war-with-openai-looms/
- 사용 한도 타임라인(explainx): https://www.explainx.ai/blog/claude-usage-limits-2026-timeline-explained
- 5시간 2배·주간 상한(morphllm): https://www.morphllm.com/claude-code-usage-limits
- Rate limit 세부(truefoundry): https://www.truefoundry.com/blog/claude-code-limits-explained
- 정기 실행 문서(Routines/cron): https://code.claude.com/docs/en/scheduled-tasks
- cron 예시(MindStudio): https://www.mindstudio.ai/blog/claude-code-cron-jobs-schedule-agents
- agent-cron: https://github.com/T0UGH/agent-cron
- 헤드리스 자동화(hidekazu-konishi): https://hidekazu-konishi.com/entry/claude_code_cicd_and_headless_automation.html
- 텔레그램 공식 플러그인: https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/README.md
- 텔레그램 커뮤니티: https://github.com/RichardAtCT/claude-code-telegram , https://github.com/linuz90/claude-telegram-bot , https://github.com/hanxiao/claudecode-telegram , https://github.com/Nickqiaoo/chatcode
- Codex CLI 문서: https://developers.openai.com/codex/cli
- Codex 인증(헤드리스/CI): https://developers.openai.com/codex/auth , https://developers.openai.com/codex/auth/ci-cd-auth
- Codex 헤드리스 인증 이슈: https://github.com/openai/codex/issues/3820
- Codex를 ChatGPT 플랜으로 사용(공식): https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- ChatGPT Pro Codex 한도(SimpleMetrics): https://simplemetrics.xyz/chatgpt-codex-limits-2026/
- OpenAI 서비스 약관: https://openai.com/policies/service-terms/
- Anthropic ToS 해설(autonomee): https://autonomee.ai/blog/claude-code-terms-of-service-explained/
- 서드파티 접근 금지 명확화(The Register): https://www.theregister.com/software/2026/02/20/anthropic-clarifies-ban-on-third-party-tool-access-to-claude/
- 서드파티 하네스 크랙다운(VentureBeat): https://venturebeat.com/technology/anthropic-cracks-down-on-unauthorized-claude-usage-by-third-party-harnesses
