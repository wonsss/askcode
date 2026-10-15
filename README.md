# AskCode

기획자가 슬랙에서 질문하면 비공개 코드베이스와 기획 문서(레포 docs·노션·슬랙 채널)를 근거로 현재 로직·화면·정책을 답하는 슬랙 봇입니다. 모든 답변에 `파일:라인` 인용을 붙이고, 인용은 봇이 도구로 실제 확인한 구간인지 코드로 검증합니다.

- 구현 계획: [docs/implementation-plan.md](docs/implementation-plan.md)
- 배포: [docs/deploy-vultr.md](docs/deploy-vultr.md)
- 데모 시나리오: [docs/demo-scenario.md](docs/demo-scenario.md)
- 비용 모델과 실측: [docs/cost.md](docs/cost.md)
- 와이어프레임 생성: [docs/wireframe.md](docs/wireframe.md)
- 웹 콘솔 기획(미구현): [docs/web-console.md](docs/web-console.md)
- 정책·흐름 답변의 로직 흐름도: [docs/flowchart.md](docs/flowchart.md)
- 데모 대상: [wonsss/law-watch](https://github.com/wonsss/law-watch) (main)

## 구조

```
슬랙 (Socket Mode, 멘션·DM·스크린샷)
  └ 질의 파이프라인
     1. 스코핑   소형 모델 + 도메인 맵(config/domains.yaml) → 범위·유형·확신도
     2. 라우팅   거절 · 되묻기(버튼) · 단순(소형 모델) · 에이전트(대형 모델) · 와이어프레임(대형 모델)   ← 코드로 강제
     3. 에이전트 Claude Agent SDK, 내장 도구 끔, 읽기 전용 도구만, 범위 하드 필터(1회 확장)
        ├ find_screen  화면이 네이티브/웹뷰/외부 중 무엇인지, 화면 파일, 직접 호출 API
        ├ find_api     api-client 함수 → NestJS 컨트롤러 연결
        ├ find_bridge  웹뷰 ↔ 앱 메시지 송수신 위치
        ├ code_search / read_code / list_dir
        └ search_docs / read_doc   레포 docs · 노션 · 슬랙 지정 채널
     4. 인용 검증 인덱스 존재 · 라인 범위 · 도구로 실제 본 구간 → 확신도
  └ 감사 로그 (SQLite, append-only)

동기화 (10분 주기): GitHub App → 얕은 클론 → 제외 규칙 → 코드 인덱스 · 구조 맵 · 문서 인덱스
                     노션(내부 통합) · 슬랙(지정 공개 채널, 최근 90일) 수집
```

## 실행

```bash
uv sync
cp .env.example .env            # 값 채우기
uv run askcode sync             # 레포 클론 + 코드·구조·문서 인덱싱 + 노션·슬랙 수집
uv run askcode ask "앱에서 의안 상세 화면은 네이티브야 웹뷰야?"
uv run askcode ask "알림 로직 알려줘"                      # 되묻기 → 후보 도메인 출력
uv run askcode ask "알림 로직 알려줘" --domains follows-push
uv run askcode ask "커뮤니티 기능 추가 시 와이어프레임 만들어줘"   # → data/wireframes/*.html
uv run askcode eval             # 골든 세트 28문항 평가 → eval/results/eval-*.md
uv run askcode serve            # 슬랙 봇
uv run askcode stats            # 질문 수·비용·경로·확신도·피드백·범위 미스
uv run pytest
```

## 슬랙 앱 설정

0. 가장 빠른 방법: api.slack.com/apps → Create New App → **From a manifest** → `slack-app-manifest.yaml` 붙여 넣기 (아래 2~4단계가 자동 설정됨)
1. 앱 생성 → **Socket Mode** 켜기 → App-Level Token(`connections:write`) → `ASKCODE_SLACK_APP_TOKEN`
2. Bot Token Scopes
   - 기본: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
   - 스크린샷 역추적: `files:read`
   - 와이어프레임 HTML 파일 올리기: `files:write` (추가한 뒤 **앱을 워크스페이스에 다시 설치**해야 적용)
   - 슬랙 채널 소스: `channels:history`, `channels:read`, `users:read`
3. Event Subscriptions: `app_mention`, `message.im`
4. Interactivity 켜기 (Socket Mode라 Request URL 불필요)
5. 워크스페이스에 설치 → Bot User OAuth Token → `ASKCODE_SLACK_BOT_TOKEN`
6. 소스로 쓸 공개 채널에 봇 초대 → 채널 ID를 `ASKCODE_SLACK_SOURCE_CHANNELS`에

## 노션 설정

노션 화면 기준 2026-10. 예전 "내부 통합"이 지금은 **연결 → API 토큰**이다.

1. 노션 **설정 → 연결 → 개발자 도구**(app.notion.com/developers/connections) → **신규 연결**
   - 연결 이름 `AskCode`, 인증 방법 **API 토큰** (워크스페이스 1개 전용. OAuth는 필요 없음)
2. 만든 연결의 **구성** 탭
   - 기능은 **콘텐츠 읽기**만 켠다. 업데이트·삽입·댓글 작성은 끈다
   - 토큰(`ntn_…`)을 `.env`의 `ASKCODE_NOTION_TOKEN`에 넣는다
3. 기획 문서 최상위 페이지 → 오른쪽 위 **⋯ → 연결 → 연결 추가** → `AskCode` (하위 페이지는 자동 포함)
   - 연결의 **콘텐츠 사용 권한** 탭에 페이지가 보이면 성공
4. 최상위 페이지 ID(주소 끝 32자리)를 `.env`의 `ASKCODE_NOTION_ROOT_PAGE_ID`에 넣는다
5. `uv run askcode doctor --online`에서 "노션 ✅ 최상위 페이지 접근 가능" 확인 → `uv run askcode sync`

주의

- API 토큰 연결은 **만든 워크스페이스의 페이지만** 읽는다. 문서가 다른 워크스페이스에 있으면 그 워크스페이스에서 연결을 만들거나 문서를 옮긴다.
- 페이지를 다른 워크스페이스로 옮기면 **페이지 ID가 바뀐다**. `.env`와, 노션 경로를 근거로 쓰는 `eval/golden.yaml`도 함께 고친다(파일 이름 끝 8자리가 페이지 ID).

## 확인 요청 채널 (선택)

답변의 "개발자에게 확인 요청" 버튼이 글을 올릴 채널이다.

1. 공개 채널을 만들고 봇을 초대한다 (`/invite @AskCode`)
2. 채널 ID(C로 시작)를 `.env`의 `ASKCODE_ESCALATION_CHANNEL`에 넣고 봇을 재시작한다

## 설정 파일

| 파일 | 내용 |
| --- | --- |
| `config/repos.yaml` | 대상 레포, 기준 ref, 제외 규칙 |
| `config/domains.yaml` | 기획자 관점 도메인 13개와 도메인별 허용 경로 |
| `config/glossary.yaml` | 기획 용어 → 코드 식별자 힌트 |
| `eval/golden.yaml` | 골든 세트 28문항 (정답·근거 파일·기대 행동) |

## 보안

- 와이어프레임은 모델이 낸 화면 명세(JSON)를 코드가 HTML로 그립니다. 모델이 쓴 값은 모두 이스케이프한 텍스트로만 들어가고, 외부 접근을 막는 CSP를 넣어 올립니다. 명세 대신 HTML이 오면 허용 목록에 있는 태그·속성만 남기고 스크립트·외부 리소스·이벤트 속성·`javascript:` 링크를 지웁니다(`pipeline/wireframe.py`).

- 봇은 코드·문서·슬랙 모두 읽기 전용입니다. 에이전트의 내장 도구(Bash, 파일 편집 등)는 꺼져 있고 허용 목록 밖 도구는 훅에서 차단합니다.
- git 추적 파일만 인덱싱하며, `.env*`·키 파일은 설정과 무관하게 항상 차단합니다(`.env.example`만 허용).
- GitHub 토큰은 git 명령마다 헤더로만 전달하고 `.git/config`에 남기지 않습니다.
- 스크린샷은 저장하지 않고, 추출한 화면 문구만 쓰며 사용자 데이터로 보이는 값은 가립니다.
- 슬랙 소스는 공개 채널만, 봇 메시지는 제외합니다.
- Socket Mode라 서버에 인바운드 포트가 필요 없습니다. 컨테이너는 비루트·읽기 전용·권한 제거로 실행합니다.
- 감사 로그 테이블은 트리거로 수정·삭제를 막습니다.

## 한계 (PoC)

- 구조 맵 추출기는 law-watch 구조(api-client `${V1}` 경로, NestJS 데코레이터, Expo Router, route manifest)에 맞춰져 있습니다. 다른 레포는 `StructureConfig`를 조정해야 합니다.
- 단순 호출(스코핑·판정·스크린샷) 비용은 설정한 단가로 추정합니다. 에이전트 비용은 SDK가 보고한 값입니다.
- 웹 콘솔은 설계 문서에만 있습니다.
- 와이어프레임은 저충실도 HTML 한 장입니다(화면 명세 → 렌더러). 실제 디자인 시스템·컴포넌트를 쓰지 않고, 기존 화면의 구성 관례(탭·헤더·목록)만 따릅니다. 이미지로 미리보기를 만들지 않으므로 파일을 내려받아 브라우저로 엽니다.
