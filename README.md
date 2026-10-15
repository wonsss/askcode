# AskCode

기획자가 슬랙에서 질문하면 비공개 코드베이스와 기획 문서를 근거로 현재 로직·화면·정책을 답하는 슬랙 봇입니다. 모든 답변에 `파일:라인` 인용을 붙이고, 인용은 봇이 도구로 실제 확인한 구간인지 코드로 검증합니다.

- 구현 계획: [docs/implementation-plan.md](docs/implementation-plan.md)
- 데모 대상: [wonsss/law-watch](https://github.com/wonsss/law-watch) (main)

## 구조

```
슬랙 (Socket Mode) → 질의 파이프라인 → Claude 에이전트 (읽기 전용 도구만)
                         │                 ├ code_search (ripgrep)
                         │                 ├ read_code (구간 읽기, 최대 200줄)
                         │                 └ list_dir
                         ├ 인용 검증 (인덱스 존재·라인 범위·실제 확인 구간)
                         └ 감사 로그 (SQLite, append-only)
레포 동기화: GitHub App(읽기 전용) → 얕은 클론 → 제외 규칙 → 파일 인덱스
```

## 실행

```bash
uv sync
cp .env.example .env      # 값 채우기
uv run askcode sync       # 레포 클론 + 인덱싱
uv run askcode ask "앱에서 의안 상세 화면은 네이티브야 웹뷰야?"
uv run askcode serve      # 슬랙 봇 실행
uv run askcode stats      # 질문 수·비용·확신도·피드백 요약
uv run pytest
```

## 슬랙 앱 설정

1. api.slack.com에서 앱 생성 → **Socket Mode** 켜기 → App-Level Token(`connections:write`) 발급 → `ASKCODE_SLACK_APP_TOKEN`
2. Bot Token Scopes: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
3. Event Subscriptions: `app_mention`, `message.im`
4. Interactivity 켜기 (Socket Mode라 Request URL 불필요)
5. 워크스페이스에 설치 → Bot User OAuth Token → `ASKCODE_SLACK_BOT_TOKEN`

## 보안

- 봇은 코드·문서·슬랙 모두 읽기 전용입니다. 에이전트의 내장 도구(Bash, 파일 편집 등)는 꺼져 있고 허용 목록 밖 도구는 훅에서 차단합니다.
- git 추적 파일만 인덱싱하며, `.env*`·키 파일은 설정과 무관하게 항상 차단합니다(`.env.example`만 허용).
- GitHub 토큰은 git 명령마다 헤더로만 전달하고 `.git/config`에 남기지 않습니다.
- Socket Mode라 서버에 인바운드 포트가 필요 없습니다.
- 감사 로그 테이블은 트리거로 수정·삭제를 막습니다.
