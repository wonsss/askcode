# AskCode

기획자가 슬랙에서 질문하면 비공개 코드베이스와 기획 문서(레포 docs·노션·슬랙 채널)를 근거로 현재 로직·화면·정책을 답하는 슬랙 봇입니다. 모든 답변에 `파일:라인` 근거를 붙이고, 그 근거가 봇이 도구로 실제 확인한 구간인지 코드로 검증합니다. 답은 웹 콘솔에 쌓여 팀이 다시 찾아 씁니다.

데모 대상: [wonsss/law-watch](https://github.com/wonsss/law-watch) (main) — Next.js 웹, Expo 하이브리드 앱(네이티브 + 웹뷰), NestJS API·워커 모노레포.

## 할 수 있는 것

| 기능 | 예시 질문 | 결과 |
| --- | --- | --- |
| 빠른 답 | 앱 하단 탭에는 어떤 메뉴들이 있어요? | 소형 모델, 약 10초, 약 $0.003 |
| 심층 탐색 | 앱 웹뷰 상세에서 팔로우 버튼을 누르면 어떤 흐름으로 저장돼요? | 웹 → 앱 → API까지 레이어별 근거, 15~30초, 약 $0.05 |
| 되묻기 | 알림 로직 알려줘 | 탐색 없이 도메인 버튼 제시($0.0003), 고르면 그 범위로 답 |
| 기획서·코드 불일치 | 매일 받기 푸시는 몇 시에 와요? 기획서랑 같아요? | 노션 기획서와 코드를 함께 인용하고 ⚠️ 불일치 지적 |
| 로직 흐름도 | 푸시 대기 중에 알림을 끄면 어떻게 돼요? | 조건이 2개 이상이면 근거가 달린 흐름도 HTML을 스레드에 첨부 |
| 와이어프레임 | 커뮤니티 기능 추가 시 와이어프레임 만들어줘 | 기존 화면 관례를 따른 저충실도 HTML, 약 $0.04 |
| 스크린샷 | (화면 캡처 첨부) 이 화면은 어떤 코드야? / 여기에 공유 버튼 추가하면? | 화면 문구로 코드 역추적, 변경 요청이면 변경안 목업 + 영향받는 코드 위치 |
| 후속 질문 | — | 답변 아래 💬 이어서 물어보기 버튼 2~3개 |
| 웹 콘솔 | — | 지난 답 검색, 최신성 배지(근거 코드가 그 뒤로 바뀌었는지), 운영 점검, 평가 이력 |

답변 아래 버튼: 근거 전체 보기 · 👍 · 👎 · 개발자에게 확인 요청(지정 채널에 올림). 답 메타 줄에는 어느 인증 경로로 답했는지(API 키 · 구독)도 표시합니다.

## 구조

```
슬랙 (Socket Mode, 멘션·DM·스크린샷)
  └ 질의 파이프라인
     1. 스코핑   소형 모델(thinking 끔) + 도메인 맵 → 유형·범위·확신도
     2. 라우팅   거절 · 미지원 · 외부 조사 안내 · 되묻기 · 빠른 답(소형) · 심층 탐색(대형) · 와이어프레임(대형)   ← 코드로 강제
     3. 에이전트 Claude Agent SDK, 내장 도구 끔, 읽기 전용 도구만, 범위 하드 필터(1회 확장)
        ├ find_screen · find_api · find_bridge   화면 방식, API 연결, 웹뷰↔앱 메시지
        ├ code_search · read_code · list_dir
        └ search_docs · read_doc                 레포 docs · 노션 · 슬랙 지정 채널
     4. 근거 검증 인덱스 존재 · 줄 범위 · 도구로 실제 본 구간(폴더는 list_dir) → 확신도
     5. 후처리   흐름도·화면 명세 → 결정적 렌더러(HTML·SVG), 후속 질문 → 버튼
  └ 감사 로그 (SQLite, append-only) ──→ 웹 콘솔 (읽기 전용)

동기화 (10분 주기): GitHub App → 얕은 클론 → 코드 인덱스 · 구조 맵 · 문서 인덱스 · 커밋별 파일 해시
                     노션(API 토큰 연결) · 슬랙(지정 공개 채널, 최근 90일) 수집
```

<img src="docs/screenshots/web-console-home.webp" alt="웹 콘솔 홈" width="640">

*웹 콘솔 홈: 슬랙에서 받은 답을 다시 찾고, 이번 주 질문·비용·확인 필요 건수를 본다.*

## 실행

```bash
uv sync
cp .env.example .env                 # 값 채우기
uv run askcode doctor --online       # 환경·키·크레딧·노션·DB·백업 점검
uv run askcode sync                  # 레포 클론 + 인덱싱 + 노션·슬랙 수집 (Claude 비용 없음)
uv run askcode serve                 # 슬랙 봇
uv run askcode console               # 웹 콘솔 http://127.0.0.1:8787 (읽기 전용, 비용 없음)
                                     # 팀 공개: https://askcode.lawwatch.kr (Cloudflare Tunnel + Access 이메일 인증, docs/operations.md)
uv run askcode host on|off|status    # 맥북 호스트 모드: 봇·콘솔 상시 실행, 잠자기 방지, 매일 백업
uv run askcode provider [api|subscription]   # 인증 경로 확인·전환 (subscription=로컬 claude CLI 구독)

uv run askcode ask "앱에서 의안 상세 화면은 네이티브야 웹뷰야?"
uv run askcode ask "알림 로직 알려줘" --domains follows-push
uv run askcode ask "커뮤니티 기능 추가 시 와이어프레임 만들어줘"   # → data/wireframes/*.html
uv run askcode eval [--ids q01,q26]  # 골든 세트 29문항 → eval/results/ (전체 1회 약 $0.85)
uv run askcode stats

uv run askcode backup                # 감사 로그 백업 (+ 서버 밖 복사)
uv run askcode restore-check         # 최근 백업 복구 확인
uv run askcode maintenance           # 오래된 커밋 스냅숏·WAL 정리
uv run askcode redact <query_id> --reason "…"   # 개인정보 유입 시 텍스트 가림
uv run pytest
```

`ask`·`eval`·`serve`만 Claude 비용이 듭니다 — `api` 모드는 API 크레딧, `subscription` 모드는 로컬 구독 한도(표시되는 $ 값은 토큰×공식 단가 환산 추정치로 실제 과금 없음). 경로별 비용은 [docs/cost.md](docs/cost.md)에 있습니다.

## 맥북 호스트 모드 (데모·소규모)

맥북을 서버처럼 쓴다. VPS + Docker 대신 macOS `launchd`가 봇·콘솔을 띄우고 관리한다.

| | `askcode host on` 이후 |
| --- | --- |
| 실행 | 봇(`serve`)·웹 콘솔(`console`)이 바로 뜨고, 로그인할 때마다 자동으로 켜진다 |
| 장애 | 죽으면 1분 안에 다시 뜬다(키·크레딧 문제로 바로 죽으면 1분에 한 번만 재시도) |
| 잠자기 | 봇이 도는 동안 시스템 잠자기를 막는다(화면은 꺼져도 됨). 전원 연결 권장, 덮개를 닫으면 멈출 수 있다 |
| 백업 | 매일 03:30 `backup → restore-check → maintenance` |
| 중복 | 봇은 하나만 뜬다(같은 질문에 답이 두 번 가지 않도록 잠금) |
| 로그 | `data/logs/{bot,console,backup}.log` |

- 끌 때는 `askcode host off`. 등록을 지우고 잠자기 방지도 풀린다. 상태는 `askcode host status`.

### 메뉴 막대 앱 (AskCode.app)

```bash
bash scripts/build-macos-app.sh     # 빌드 → ~/Applications/AskCode.app 설치·실행 (Xcode Command Line Tools 필요)
```

메뉴 막대 아이콘을 누르면 창이 열린다.

<img src="docs/screenshots/menubar-app.webp" alt="AskCode 메뉴 막대 앱" width="360">

| 영역 | 내용 |
| --- | --- |
| 머리 | 봇 상태(🟢 실행 중 · 🟠 재시작 대기 · ⚪ 꺼짐) + 현재 인증 경로(구독·API), **켜기/끄기**(`host on/off`) |
| 작업 | 봇 · 웹 콘솔 · 백업(매일 03:30) 각각의 상태 |
| 숫자 | 오늘 질문, 오늘 비용, 이번 주 비용, 확인 필요(누르면 콘솔 `/check`) |
| 최근 질문 | 슬랙 질문 5개(경로·프로바이더·확신도·비용·⚠︎), 누르면 콘솔 상세 |
| 아래 | 마지막 백업·지금 백업, 인증 경로 토글(API \| 구독)·확인 버튼, 웹 콘솔·로그 열기 |

- 앱은 화면만 맡고 `askcode status --json`(15초마다), `host on/off`, `provider`, `backup`을 부른다. 상태 조회는 Claude를 부르지 않는다.
- 봇이 예기치 않게 멈추면 macOS 알림을 띄운다. 메뉴 막대 아이콘 옆 숫자는 확인 필요 건수다.
- 인증 경로는 `.env`의 `ASKCODE_LLM_PROVIDER`가 정한다. `api`(기본)는 `ANTHROPIC_API_KEY`로 직접 호출하고, `subscription`은 로컬 `claude` CLI의 구독 로그인을 하위 프로세스로 띄워 쓴다(SlackBotManager 방식). 메뉴 막대의 API|구독 토글 또는 `askcode provider api|subscription`으로 바꾸며, 봇이 켜져 있으면 봇만 재시작해 바로 적용된다. 실제로 답에 쓰인 경로는 답변마다 `query_results.provider`에 기록돼 슬랙 메타 줄·콘솔 칩·앱 최근 질문에 표시된다(이전 답변은 비어 있음).
- 인증은 `.env`의 Claude API 키다. 셸 환경을 물려받지 않아 셸에 남은 `ANTHROPIC_*` 값이 끼어들지 않는다(구독 모드에서는 하위 `claude` 프로세스에 이 변수들을 빈 값으로 덮어써 구독 로그인이 선택되게 한다).
- 맥북이 꺼져 있으면 봇도 멈춘다. 팀이 매일 쓰는 24시간 운영은 VPS([docs/deploy-vultr.md](docs/deploy-vultr.md))가 맞다.
- Claude Pro·Max 구독 로그인으로 타인에게 봇을 서비스하는 것은 Anthropic 정책상 허용되지 않는다 — `subscription` 모드는 이 맥북에서의 로컬 PoC·데모 전용이다.

## 문서

| 문서 | 내용 |
| --- | --- |
| [implementation-plan.md](docs/implementation-plan.md) | 설계 결정과 구현 현황 |
| [demo-scenario.md](docs/demo-scenario.md) | 7분 데모 순서, 질문 Q1~Q9와 기대 답·실측 |
| [cost.md](docs/cost.md) | 비용 계산 방법, 경로별 실측, 월 비용 추정, 모델·effort 판단 |
| [web-console.md](docs/web-console.md) | 웹 콘솔 기획과 v1 범위 |
| [wireframe.md](docs/wireframe.md) | 와이어프레임·변경안 목업 |
| [flowchart.md](docs/flowchart.md) | 정책 답변의 로직 흐름도 |
| [operations.md](docs/operations.md) | DB 운영: 보존 정책, 백업·복구, 가리기, 스키마 변경 |
| [deploy-vultr.md](docs/deploy-vultr.md) | 배포 |

## 슬랙 앱 설정

0. 가장 빠른 방법: api.slack.com/apps → Create New App → **From a manifest** → `slack-app-manifest.yaml` 붙여 넣기
1. **Socket Mode** 켜기 → App-Level Token(`connections:write`) → `ASKCODE_SLACK_APP_TOKEN`
2. Bot Token Scopes
   - 기본: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
   - 스크린샷: `files:read` · 흐름도·와이어프레임 파일 올리기: `files:write`
   - 슬랙 채널 소스: `channels:history`, `channels:read`, `users:read`
   - 권한을 추가하면 **앱을 워크스페이스에 다시 설치**해야 적용된다
3. Event Subscriptions: `app_mention`, `message.im` · Interactivity 켜기(Request URL 불필요)
4. 설치 → Bot User OAuth Token → `ASKCODE_SLACK_BOT_TOKEN`
5. (선택) 소스 채널에 봇 초대 → `ASKCODE_SLACK_SOURCE_CHANNELS`
6. (선택) 확인 요청 채널: 공개 채널에 봇 초대 → 채널 ID(C…)를 `ASKCODE_ESCALATION_CHANNEL`

봇은 질문을 받을 때 "질문과 답은 팀 콘솔에 공개돼요"를 함께 보여 줍니다(DM 포함).

## 노션 설정

노션 화면 기준 2026-10. 예전 "내부 통합"이 지금은 **연결 → API 토큰**이다.

1. 노션 **설정 → 연결 → 개발자 도구**(app.notion.com/developers/connections) → **신규 연결**, 이름 `AskCode`, 인증 방법 **API 토큰**
2. **구성** 탭: 기능은 **콘텐츠 읽기**만 켠다 · 토큰(`ntn_…`)을 `ASKCODE_NOTION_TOKEN`에
3. 기획 문서 최상위 페이지 → **⋯ → 연결 → 연결 추가** → `AskCode` (하위 페이지 자동 포함)
4. 최상위 페이지 ID(주소 끝 32자리)를 `ASKCODE_NOTION_ROOT_PAGE_ID`에 → `doctor --online` → `sync`

API 토큰 연결은 만든 워크스페이스의 페이지만 읽는다. 페이지를 다른 워크스페이스로 옮기면 페이지 ID가 바뀌므로 `.env`와 `eval/golden.yaml`의 노션 경로(파일 이름 끝 8자리)도 고친다.

## 설정

| 파일 / 환경변수 | 내용 |
| --- | --- |
| `config/repos.yaml` | 대상 레포, 기준 ref, 제외 규칙 |
| `config/domains.yaml` | 기획자 관점 도메인 13개와 도메인별 허용 경로 |
| `config/glossary.yaml` | 기획 용어 → 코드 식별자 힌트 |
| `eval/golden.yaml` | 골든 세트 29문항 (정답·근거·기대 행동) |
| `ASKCODE_LLM_PROVIDER` | `api`(기본, `ANTHROPIC_API_KEY`) \| `subscription`(로컬 `claude` CLI 구독 로그인) |
| `ASKCODE_MODEL_LARGE` / `_SMALL` | 기본 `claude-sonnet-5-5` / `claude-haiku-5-5` |
| `ASKCODE_AGENT_EFFORT_LARGE` / `_SMALL` | 비우면 모델 기본값 |
| `ASKCODE_AGENT_MAX_BUDGET_USD` | 질문 하나의 비용 상한 (기본 0.5). 구독 모드에서는 미적용 — `ASKCODE_AGENT_MAX_TURNS`만 제한 |
| `ASKCODE_BACKUP_DIR` · `_KEEP_DAYS` · `_UPLOAD_CMD` | 백업 위치, 로컬 보관 일수, 서버 밖 복사 명령(`{file}`) |

## 데이터와 보존

- 모든 데이터는 `data/askcode.sqlite3` 한 파일에 있다(`data/`, `backups/`는 git 제외).
- **감사 로그는 영구 보존한다.** 수정·삭제는 트리거가 막고, 개인정보가 섞이면 `redact`로 텍스트만 가린다.
- 배포 시 매일 백업 → 복구 확인 → 정리를 돌리고, 서버 밖으로 복사한다. 절차는 [docs/operations.md](docs/operations.md).

## 보안

- 봇은 코드·문서·슬랙 모두 읽기 전용이다. 에이전트의 내장 도구(Bash, 파일 편집 등)는 꺼져 있고, 허용 목록 밖 도구는 훅에서 차단한다.
- git 추적 파일만 인덱싱하고, `.env*`·키 파일은 설정과 무관하게 항상 차단한다(`.env.example`만 허용).
- GitHub 토큰은 git 명령마다 헤더로만 전달하고 `.git/config`에 남기지 않는다.
- 셸에 남은 `ANTHROPIC_API_KEY`·주소 설정이 `.env`의 봇 키를 덮어쓰지 않는다.
- 스크린샷은 저장하지 않는다. 화면 문구만 쓰고, 사용자 데이터로 보이는 값은 가린다.
- 흐름도·와이어프레임은 모델이 낸 명세(JSON)를 코드가 그린다. 모델 값은 이스케이프한 텍스트로만 들어가고 외부 접근을 막는 CSP를 넣는다. 명세 대신 HTML이 오면 허용 목록 정제기로 스크립트·외부 리소스·이벤트 속성을 지운다.
- 웹 콘솔은 127.0.0.1에만 열리고, 감사 로그를 읽기 전용(`mode=ro`)으로 연다. 모든 출력은 이스케이프하고 CSP·`no-referrer`를 건다.
- Socket Mode라 서버에 인바운드 포트가 필요 없다. 컨테이너는 비루트·읽기 전용·권한 제거로 실행한다.
- 감사 로그 테이블은 트리거로 수정·삭제를 막는다.

## 현재 상태와 한계

- 골든 세트 2차 평가 86%(24/28), 이후 노션 불일치 3문항 통과. 그 뒤 프롬프트를 여러 번 바꿔 **28문항 전체는 다시 돌리지 않았다.**
- 같은 질문도 스코핑이 빠른 답과 심층 탐색 사이에서 가끔 다르게 분류한다. 그래서 질문당 비용이 달라질 수 있다.
- 구조 맵 추출기는 law-watch 구조(api-client `${V1}` 경로, NestJS 데코레이터, Expo Router, route manifest)에 맞춰져 있다. 다른 레포는 `StructureConfig`를 조정해야 한다.
- 와이어프레임은 저충실도다. 실제 디자인 시스템을 쓰지 않고 기존 화면의 구성 관례만 따른다.
- 웹 콘솔 P1(산출물 보관, 도메인 FAQ, 슬랙에서 기존 답 먼저 제안 등)과 VPS 배포는 아직이다. Docker 이미지 빌드도 확인하지 않았다.
