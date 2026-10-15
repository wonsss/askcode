# AskCode

기획자가 슬랙에서 질문하면 비공개 코드베이스와 기획 문서(레포 docs·노션·슬랙 채널)를 근거로 현재 로직·화면·정책을 답하는 슬랙 봇입니다. 모든 답변에 `파일:라인` 인용을 붙이고, 인용은 봇이 도구로 실제 확인한 구간인지 코드로 검증합니다.

- 구현 계획: [docs/implementation-plan.md](docs/implementation-plan.md)
- 배포: [docs/deploy-vultr.md](docs/deploy-vultr.md)
- 데모 시나리오: [docs/demo-scenario.md](docs/demo-scenario.md)
- 데모 대상: [wonsss/law-watch](https://github.com/wonsss/law-watch) (main)

## 구조

```
슬랙 (Socket Mode, 멘션·DM·스크린샷)
  └ 질의 파이프라인
     1. 스코핑   소형 모델 + 도메인 맵(config/domains.yaml) → 범위·유형·확신도
     2. 라우팅   거절 · 되묻기(버튼) · 단순(소형 모델) · 에이전트(대형 모델)   ← 코드로 강제
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
uv run askcode eval             # 골든 세트 28문항 평가 → eval/results/eval-*.md
uv run askcode serve            # 슬랙 봇
uv run askcode stats            # 질문 수·비용·경로·확신도·피드백·범위 미스
uv run pytest
```

## 슬랙 앱 설정

1. api.slack.com에서 앱 생성 → **Socket Mode** 켜기 → App-Level Token(`connections:write`) → `ASKCODE_SLACK_APP_TOKEN`
2. Bot Token Scopes
   - 기본: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
   - 스크린샷 역추적: `files:read`
   - 슬랙 채널 소스: `channels:history`, `channels:read`, `users:read`
3. Event Subscriptions: `app_mention`, `message.im`
4. Interactivity 켜기 (Socket Mode라 Request URL 불필요)
5. 워크스페이스에 설치 → Bot User OAuth Token → `ASKCODE_SLACK_BOT_TOKEN`
6. 소스로 쓸 공개 채널에 봇 초대 → 채널 ID를 `ASKCODE_SLACK_SOURCE_CHANNELS`에

## 노션 설정

1. 노션 설정 → 연결(Integrations) → 내부 통합 생성, 권한은 **콘텐츠 읽기만**
2. 기획 문서 최상위 페이지에서 ⋯ → 연결 → 위 통합 추가 (하위 페이지 자동 포함)
3. 토큰과 최상위 페이지 ID를 `.env`에

## 설정 파일

| 파일 | 내용 |
| --- | --- |
| `config/repos.yaml` | 대상 레포, 기준 ref, 제외 규칙 |
| `config/domains.yaml` | 기획자 관점 도메인 13개와 도메인별 허용 경로 |
| `config/glossary.yaml` | 기획 용어 → 코드 식별자 힌트 |
| `eval/golden.yaml` | 골든 세트 28문항 (정답·근거 파일·기대 행동) |

## 보안

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
- 웹 콘솔·와이어프레임 생성은 설계 문서에만 있습니다.
