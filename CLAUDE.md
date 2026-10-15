# CLAUDE.md

AskCode: 기획자가 슬랙에서 질문하면 비공개 코드베이스(law-watch)와 기획 문서(레포 docs·노션·슬랙 채널)를 근거로 답하는 슬랙 봇. 면접용 PoC이며 라이브 데모와 문서 공유가 목표다.

## 먼저 읽을 것

- `README.md` — 구조, 실행 명령, 슬랙·노션 설정
- `docs/implementation-plan.md` — 설계 결정과 구현 현황(0절)
- `docs/demo-scenario.md` — 7분 데모 순서, 노션 문서의 의도적 불일치 3곳
- `docs/deploy-vultr.md` — 배포
- `docs/cost.md` — 경로별 비용 실측, 월 비용 추정, 모델·effort 판단

## 명령

```bash
uv run askcode doctor --online   # 환경·연결 점검
uv run askcode sync              # 레포 동기화 + 코드·구조·문서 인덱싱 + 노션·슬랙 수집
uv run askcode ask "질문" [--domains follows-push]
uv run askcode eval [--ids q01,q26] [--limit N]   # 골든 세트 28문항 → eval/results/
uv run askcode serve             # 슬랙 봇 (Socket Mode)
uv run askcode stats
uv run pytest                    # 50개
```

## 구조 요약

`src/askcode/`
- `app.py` 조립 지점 · `cli.py` · `doctor.py` · `evaluation.py`
- `pipeline/` 스코핑(`scoping.py`) → 라우팅(거절·되묻기·단순·에이전트·와이어프레임) → `answer.py`(인용 검증·확신도) · `prompts.py` · `screenshot.py` · `wireframe.py`(HTML 추출·허용 목록 정제·CSP)
- `agent/` `AgentRunner` 포트 + Claude Agent SDK 구현(내장 도구 끔, 읽기 전용 도구만, PreToolUse 훅 차단)
- `tools/` code_search·read_code·list_dir · find_screen·find_api·find_bridge · search_docs·read_doc
- `indexing/` repo_sync(GitHub App) · code_index · structure(API·라우트·manifest·브리지 맵) · docs_index · notion_source · slack_source
- `store/db.py` SQLite. 감사 로그 테이블은 트리거로 append-only
- `config/` repos.yaml · domains.yaml(13개 도메인) · glossary.yaml · `eval/golden.yaml`(28문항)

## 지켜야 할 것

- 봇은 모든 소스에 **읽기 전용**이다. 에이전트에 쓰기·셸·외부 전송 도구를 추가하지 않는다.
- `.env`, `secrets/`, `data/`는 커밋하지 않는다(.gitignore). 비밀값을 출력·로그·커밋 메시지에 남기지 않는다.
- law-watch는 **비공개 레포**다. `eval/golden.yaml`·`docs/`에 law-watch 내부 정보가 있으므로 AskCode 레포는 비공개로 유지한다.
- 커밋은 작업 단위로 나누고, 파일 경로를 명시해 스테이징한다(`git add -A` 금지). push는 사용자 확인 후에만.
- 골든 세트 정답을 바꿀 때는 law-watch 코드를 직접 읽어 근거 라인을 확인한다.

## 현재 상태 (2026-10-15)

- 테스트 50개 통과. 실제 Claude API·슬랙으로 실측 완료. 골든 세트 2차 평가 86%(24/28), 이후 노션 3문항(q26~q28) 통과. 단, 답변 문체를 바꾼 뒤 28문항 전체를 다시 돌리지는 않았다.
- 비용은 SDK 보고값이 아니라 토큰 × 단가로 계산한다(`llm/pricing.py`). 경로별 실측과 월 비용 추정은 `docs/cost.md`.
- **API 비용이 드는 작업(eval·ask)은 예상 금액을 먼저 사용자에게 말한다.** 전체 평가 1회 약 $0.85.
- `.env` 입력 완료: Claude 키, 슬랙 봇·앱 토큰, GitHub App(ID·설치 ID·`secrets/github-app.pem`). GitHub App으로 law-watch 클론·인덱싱 확인됨(`data/`, main@31bcf1e). 확인 요청 채널(`ASKCODE_ESCALATION_CHANNEL`)도 설정됨.
- 노션 데모 문서: 「AskCode 데모 — law-watch 기획 문서」(page id `594f6bc1093b83fb9cfe8154b27d9d2e`, 장원석님의 워크스페이스 › LawWatch)와 하위 8개 페이지. 봇 전용 연결 `AskCode`(API 토큰, 콘텐츠 읽기만)로 수집됨. 원래 문서(`3fa5aab5…`)는 다른 워크스페이스에 있어 옮겼고, 그때 페이지 ID가 바뀌었다.
- 모델: `claude-sonnet-5-5`(심층 탐색·판정), `claude-haiku-5-5`(스코핑·빠른 답). 소형 모델 호출은 thinking을 끈다(켜면 스코핑 JSON이 잘림). effort는 `ASKCODE_AGENT_EFFORT_LARGE/SMALL`로 바꿀 수 있고 비우면 모델 기본값.
- 와이어프레임 생성 구현(저충실도 HTML, 슬랙 파일 업로드). 슬랙 앱에 `files:write`를 추가하고 재설치해야 업로드된다.
- 남은 실패: q11(스코핑이 가끔 되묻기로 분류), q14(웹 기본 페이지 크기 6건 누락).
- 에이전트 SDK 하위 프로세스는 부모 환경변수를 물려받는다.
