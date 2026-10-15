# AskCode 구현 계획

- 작성: 2026-10-15
- 상태: 구현 완료, 실측 대기
- 기간: 계획 4일 → 실제 1일
- 대상 코드베이스: [wonsss/law-watch](https://github.com/wonsss/law-watch) (main 브랜치)

## 0. 구현 현황 (2026-10-15)

계획한 범위를 모두 구현했습니다. 정식 API 키·슬랙·노션 토큰을 넣은 실측은 아직 없습니다(키 등록 후 `askcode eval`).

| 항목 | 상태 | 계획 대비 변경 |
| --- | --- | --- |
| 레포 동기화·코드 인덱스·제외 규칙 | 완료 | — |
| 구조 맵 (API 연결 109건, 화면 manifest 42개, 라우트 91개, 브리지 메시지) | 완료 | — |
| 도메인 맵·용어 사전·스코핑·되묻기·라우팅·범위 하드 필터 | 완료 | 도메인 13개 |
| 문서 인덱스 (레포 docs 202개 · 노션 · 슬랙 채널) | 완료 | FTS5 trigram + 2글자 한국어는 LIKE 보완 |
| 인용 검증·확신도·불일치·설정 의존 표시 | 완료 | — |
| 스크린샷 역추적 | 완료 | OCR 엔진 대신 Claude 비전으로 문구 추출 |
| 평가 하네스·골든 세트 | 완료 | 28문항 (노션 불일치 3문항 추가) |
| 노션 데모 문서 | 완료 | 8개, 의도적 불일치 3곳 |
| Docker·배포 가이드·데모 시나리오 | 완료 | 이미지 빌드는 Docker 데몬이 있는 환경에서 확인 필요 |
| 웹 콘솔·와이어프레임 | 미구현 | 범위 밖(설계만) |

참고 실측 1건: 스코핑이 실패(API 키 없음)해 범위 제한 없이 대형 모델이 레포 전체를 탐색한 경우, "알림 로직 알려줘" 한 질문에 약 $0.16·28초가 들었다. 정상이라면 이 질문은 되묻기로 끝나야 한다.

## 1. 개요

AskCode는 기획자가 슬랙에서 질문하면 비공개 코드베이스와 기획 문서를 근거로 현재 로직·화면·정책을 답하는 슬랙 봇이다. 기획자가 개발자에게 반복해서 묻던 질문을 줄이고, 모든 답변에 `파일:라인`과 문서 링크를 붙여 검증 가능하게 만드는 것이 목표다.

이번 PoC는 실제 운영 중인 하이브리드 앱 모노레포(law-watch)를 대상으로 다음을 증명한다.

1. 35만 줄 규모의 모노레포 전체에서 질문 범위를 좁혀 정확하고 저렴하게 답한다.
2. 네이티브 ↔ React Native ↔ WebView ↔ API ↔ 서버 정책을 넘나드는 흐름을 추적한다.
3. 기획 문서(노션)와 코드가 다를 때 양쪽을 인용해 불일치를 드러낸다.
4. 정확도와 비용을 골든 세트로 측정해 수치로 보여준다.

### 1.1 성공 기준

| 영역 | 지표 | 목표 |
| --- | --- | --- |
| 정확도 | 골든 세트 정답률 | 80% 이상 |
| 정확도 | 인용 정확도 (실재하는 파일·라인·링크) | 100% |
| 스코핑 | 범위 재현율 (정답 도메인이 범위에 포함) | 90% 이상 |
| 비용 | 질문당 평균 비용 | 측정 후 보고 (상한은 1일차 실측으로 설정) |
| 응답 | 단순 질문 응답 시간 | 15초 이내 |
| 데모 | 리허설 | 시나리오 전체 2회 연속 무사 통과 |

### 1.2 범위

| 포함 | 문서로만 설계 | 제외 |
| --- | --- | --- |
| 슬랙 봇 (멘션·DM, 버튼 되묻기) | 웹 콘솔, 관리자 화면 | 고충실도 화면, 실제 앱 빌드·렌더링 |
| law-watch 전체 코드 인덱싱 | 와이어프레임 생성 | 멀티 프로바이더 구현 |
| 노션 기획 문서 검색 | Contextual Retrieval, 재정렬 모델 | 고객 데이터 접근 |
| 슬랙 지정 채널 검색 (선택) | 소스 맵 자동 생성 | |
| 스코핑, 질문 라우팅, 에이전트 루프 | 작업 대기열, Postgres | |
| 인용 검증, 확신도, 감사 로그 | 대화 채널 어댑터 (Teams 등) | |
| 골든 세트 평가 하네스 | | |
| 스크린샷 역추적 (시간 여유 시) | | |

## 2. 아키텍처

```
기획자 (Slack)
   │  멘션 / DM / 버튼
   ▼
Slack Bolt (Socket Mode) ── 즉시 "확인 중…" 표시
   │
   ▼
질의 처리 파이프라인
   1. 스코핑      소형 모델 + 도메인 맵(캐시) → 범위 명세 {도메인, 경로, 문서, 확신도}
   2. 되묻기      확신도 낮음 → 후보 도메인 버튼으로 반문, 종료
   3. 라우팅      단순 조회 → 1회 검색 / 다중 홉 → 에이전트 루프
   4. 에이전트    Claude Agent SDK + 읽기 전용 도구 (범위 하드 필터)
   5. 인용 검증   파일·라인·노션 블록·슬랙 permalink 실재 여부를 코드로 검사
   6. 응답        요약 + 인용 + 확신도 + 기준 커밋, 상세 근거는 스레드 답글
   │
   ├─ 도구: code_search / read_code / trace_screen / trace_api / read_route_manifest
   │        search_docs / read_notion / search_slack / lookup_glossary
   ▼
인덱스 (SQLite)
   ├─ 코드: 파일·심볼 인덱스, API 연결 맵, 화면·라우트 맵, WebView 브리지 맵, 용어 사전
   ├─ 문서: 레포 docs/ + 노션 페이지 (FTS5 trigram, 선택적 임베딩)
   └─ 감사 로그: 질문, 범위, 도구 호출, 인용, 토큰, 비용, 피드백

외부 연결 (봇 서버의 자격 증명, 모두 읽기 전용)
   ├─ GitHub App → law-watch main 얕은 클론, 주기적 fetch로 증분 갱신
   ├─ Notion 내부 통합 토큰 → 공유받은 기획 문서 최상위 페이지 하위만
   ├─ Slack 앱 토큰 → 인터페이스 + 지정 공개 채널 최근 3개월
   └─ Claude API 키
```

### 2.1 핵심 설계 결정

| 항목 | 결정 | 이유 |
| --- | --- | --- |
| 코드 검색 | 구조 인덱스 + 에이전트 실시간 탐색, 임베딩 미사용 | 코드는 정확 일치와 호출 관계가 중요. 인덱싱 비용 0 |
| 문서 검색 | SQLite FTS5 trigram + 선택적 임베딩 | trigram은 형태소 분석 없이 한국어 부분 일치 가능. 코퍼스가 작아 별도 엔진 불필요 |
| 범위 축소 | 사람이 정의한 도메인 맵 YAML + 소형 모델 스코핑 | 자동 클러스터링보다 싸고 정확. 틀리면 조용히 범위를 놓치는 위험 차단 |
| 에이전트 | Claude Agent SDK 전용 최적화 | 캐싱·서브에이전트 등 고유 기능 활용. 교체는 포트로 격리 |
| 기준 버전 | main 브랜치, 답변에 커밋 해시 표시 | 미배포 변경 포함 가능성을 안내 |
| 저장소 | SQLite (설계상 Postgres) | 데모 규모에 충분, 운영 부담 없음 |
| 배포 | Vultr VPS 1대 + Docker Compose | Socket Mode라 인바운드 포트 불필요 |

### 2.2 확장 포트 (MVP는 3개만)

| 포트 | 1차 구현 | 교체 시나리오 |
| --- | --- | --- |
| `AgentRunner` | Claude Agent SDK | 다른 프로바이더 필요 시 구현체 추가 |
| `complete(prompt, tier, cache_prefix)` | Anthropic SDK 직접 호출 | 규제 환경이면 Bedrock 등으로 내부만 교체 |
| 소스 도구 (MCP 형태) | 코드·노션·슬랙 읽기 전용 도구 | 지라·피그마 등 도구 추가 |

모든 도구는 표준 인용 포맷 `{source, title, url, location, updated_at, commit}`을 반환한다.

## 3. 기술 스택

| 영역 | 선택 |
| --- | --- |
| 언어 | Python 3.12, uv |
| 슬랙 | `slack-bolt` (AsyncApp, Socket Mode) |
| 에이전트 | `claude-agent-sdk` |
| LLM 단순 호출 | `anthropic` SDK |
| 코드 분석 | ripgrep, tree-sitter (TypeScript·TSX·Kotlin·Swift) |
| 저장소 | SQLite (FTS5), 필요 시 `sqlite-vec` |
| 노션 | Notion API (내부 통합 토큰) |
| 설정 | pydantic-settings, `.env` (git 제외) |
| 테스트·평가 | pytest, 자체 평가 하네스 CLI |
| 배포 | Docker Compose, Vultr 서울 리전 |

## 4. 프로젝트 구조

```
askcode/
├─ docs/
│  └─ implementation-plan.md
├─ config/
│  ├─ repos.yaml           # 레포 레지스트리 (law-watch, 기준 ref, 제외 규칙)
│  ├─ domains.yaml         # 도메인 맵 (사람이 정의)
│  └─ glossary.yaml        # 용어 보강 (자동 추출분 + 수동)
├─ src/askcode/
│  ├─ app.py               # 조립 지점 (composition root)
│  ├─ settings.py
│  ├─ slack/               # 이벤트 핸들러, Block Kit 응답, 버튼
│  ├─ pipeline/            # scoping, routing, verify, respond
│  ├─ agent/               # AgentRunner 포트 + Claude 구현
│  ├─ llm/                 # complete()
│  ├─ tools/               # 읽기 전용 도구들
│  ├─ indexing/
│  │  ├─ repo_sync.py      # GitHub App 클론·fetch, 변경 파일 감지
│  │  ├─ code_index.py     # 파일·심볼 인덱스
│  │  ├─ api_map.py        # api-client endpoints ↔ NestJS 컨트롤러
│  │  ├─ route_map.py      # Next.js 페이지, Expo 라우트, public-route-manifest
│  │  ├─ bridge_map.py     # WebView 메시지 송수신 지점
│  │  ├─ glossary.py       # 한국어 문구 ↔ 식별자
│  │  └─ docs_index.py     # 레포 docs/ + 노션
│  ├─ store/               # SQLite 스키마, 감사 로그
│  └─ security/            # 제외 규칙, 마스킹, HTML·출력 정제
├─ eval/
│  ├─ golden.yaml          # 골든 세트
│  └─ run_eval.py          # 평가 하네스
├─ tests/
├─ Dockerfile
├─ docker-compose.yml
└─ .env.example
```

## 5. 인덱싱 설계 (law-watch 기준)

### 5.1 대상과 제외 규칙

실측 결과 생성물·바이너리를 뺀 인덱싱 대상은 파일 3,285개, 코드 약 35만 줄(테스트 6.7만 줄 포함), 문서 483개(약 7.8만 줄)다.

제외 규칙(`config/repos.yaml`):

```yaml
exclude:
  - "**/.env*"              # .env.example만 예외로 허용
  - "pnpm-lock.yaml"
  - "**/*.{png,jpg,jpeg,gif,webp,ico,otf,ttf,woff,woff2,tgz,pdf,lottie,mp4}"
  - "apps/worker/scripts/*-results.json"
  - "archive/**"
  - ".impeccable/**"
  - ".gstack/**"
  - ".playwright-mcp/**"
  - "**/dist/**"
  - "**/storybook-static/**"
include_exceptions:
  - "**/.env.example"
```

클론은 git 추적 파일만 대상으로 하므로 로컬 비밀값 파일은 처음부터 들어오지 않는다. 제외 규칙은 이중 안전장치다.

### 5.2 코드 인덱스

| 인덱스 | 만드는 방법 | 쓰는 곳 |
| --- | --- | --- |
| 파일·심볼 | tree-sitter로 export·class·function·컨트롤러 메서드 추출 | 정의·참조 추적 |
| API 연결 맵 | `packages/api-client/src/endpoints.ts`의 경로 문자열 ↔ `apps/api/src/**/presentation/*.controller.ts`의 `@Controller` + 메서드 데코레이터 매칭 (컨트롤러 66개) | 화면 → API → 서버 추적 |
| 화면·라우트 맵 | `apps/web/app/**/page.tsx`(61개), `apps/mobile/app/**`(32개), `apps/mobile/src/navigation/public-route-manifest.ts`의 `presentation`(native·embedded·external·web-only) | "이 화면이 네이티브야 웹뷰야?" |
| WebView 브리지 맵 | `apps/web/lib/webview-bridge.ts`의 메시지 타입(`law-watch:*`) ↔ `apps/mobile/src/web.ts` 등 `onMessage` 처리 지점 | 레이어 간 흐름 추적 |
| 정책 지점 | 상수, enum, 유효성 검사, 환경변수·설정 조회 패턴. 설정 조회에는 "외부 설정 의존" 태그 | 정책 질문, 설정 의존 표시 |
| 용어 사전 | `packages/contracts`의 타입·enum 주석, 화면 문자열, docs 제목에서 한국어 ↔ 식별자 추출 + `glossary.yaml` 보강 | 기획 용어 → 코드 용어 변환 |

모두 결정론적으로 만들어 토큰을 쓰지 않는다. 갱신은 `git fetch` 후 변경 파일만 다시 처리한다.

### 5.3 도메인 맵

API 모듈 33개를 기준으로 기획자 관점의 도메인 20개 안팎을 `config/domains.yaml`에 정의한다. 초안은 LLM이 한 번 제안하고 사람이 수정한다.

```yaml
- id: bills
  name: 의안
  summary: 의안 목록·상세·비교, 진행 단계, 원문 PDF
  paths:
    - apps/api/src/bills/**
    - apps/web/app/bills/**
    - apps/mobile/app/**/bills/**
    - packages/contracts/src/bill*.ts
  docs: [docs/decisions/*bill*, notion:의안 상세 화면 기획]
  terms: [의안, 법안, 발의, 계류, 가결]
- id: follows-push
  name: 팔로우·내 소식·푸시
  ...
```

### 5.4 문서 인덱스

| 소스 | 청크 단위 | 메타데이터 |
| --- | --- | --- |
| 레포 `docs/` | 제목(heading) 단위 섹션 | 파일 경로, 문서 상태 줄, 커밋 |
| 노션 | 제목 단위 섹션 | 페이지 경로, 최종 수정일, URL |
| 슬랙 (선택) | 스레드 | 채널, 날짜, permalink |

SQLite FTS5의 trigram 토크나이저로 한국어 부분 일치 검색을 하고, 정확도가 부족하면 임베딩 검색을 추가해 RRF로 융합한다.

## 6. 질의 처리 설계

### 6.1 스코핑과 되묻기

- 입력: 질문 + 도메인 맵 요약(프롬프트 캐시) + 용어 사전 매칭 결과
- 출력: `{domains: [...], paths: [...], docs: [...], confidence: high|medium|low, clarify_options?: [...]}`
- 확신도 low 또는 후보 3개 이상이면 탐색하지 않고 후보를 슬랙 버튼으로 제시한다.
- 범위 밖 질문(고객 데이터, 외부 서비스)은 즉시 안내하고 종료한다.

### 6.2 라우팅

| 유형 | 예 | 처리 |
| --- | --- | --- |
| 단순 조회 | "검색은 어떤 방식이야?" | 인덱스 1회 조회 + 소형 모델 답변 |
| 결정 배경 | "왜 단일 WebView로 안 바꿨어?" | 문서 검색 + 답변 |
| 다중 홉 | "앱 의안 상세에서 팔로우 누르면 어떻게 처리돼?" | 에이전트 루프 |
| 넓은 질문 | "알림 로직 알려줘" | 되묻기 |

라우팅 규칙은 코드로 강제한다.

### 6.3 에이전트 루프

- 도구는 모두 읽기 전용이며 스코핑 범위 밖 결과를 반환하지 않는다.
- 도구 결과는 상위 k개, 파일은 관련 구간과 앞뒤 일부만 반환한다. "레포 전체 나열" 도구는 두지 않는다.
- 고수준 도구 `trace_screen(route)`, `trace_api(endpoint)`로 반복 탐색을 줄인다.
- 정지 조건: 최대 도구 호출 12회, 질문당 토큰 예산. 초과 시 찾은 근거로 답하고 확신도를 낮춘다.
- 프롬프트 순서: 시스템 프롬프트 → 도구 정의 → 도메인 맵 요약 → 질문 (캐시 적중 극대화)
- 도구 호출 전후 훅으로 감사 로그를 남긴다.

### 6.4 인용 검증과 확신도

- 코드 인용: 커밋 기준 파일 존재, 라인 범위 유효, 인용 문구 일치 여부
- 노션 인용: 페이지 ID 존재, 인용 문구가 해당 블록에 실재하는지
- 실패한 인용은 제거하고 확신도를 한 단계 낮춘다.
- 확신도: high(코드 직접 인용), medium(문서 근거만), low(추론)

### 6.5 슬랙 응답 형식

아래 경로·라인·API는 형식을 보여주기 위한 예시다.

```
[확신도: 높음] 기준: main @ 31bcf1e

앱의 의안 상세는 WebView(embedded)로 웹 /embed/bills/... 를 띄웁니다.
팔로우 버튼은 ... → POST /api/v1/favorites → ... 순서로 처리됩니다.

근거
• apps/mobile/src/navigation/public-route-manifest.ts:42
• apps/web/lib/webview-bridge.ts:88
• apps/api/src/favorites/presentation/favorites.controller.ts:31

[근거 전체 보기]  [👍] [👎]  [개발자에게 확인 요청]
```

## 7. 보안

| 항목 | 방침 |
| --- | --- |
| 코드 접근 | GitHub App, Contents·Metadata 읽기 전용, law-watch 레포에만 설치 |
| 노션 접근 | 내부 통합, 콘텐츠 읽기만, 기획 문서 최상위 페이지에만 공유 |
| 슬랙 | Socket Mode(인바운드 포트 없음), 지정 공개 채널만 조회, DM·비공개 채널 제외, 봇 자신의 메시지 제외 |
| 비밀값 | 서버 `.env`에만 보관, 레포·이미지에 포함 금지, 파일 권한 600 |
| 인덱싱 | git 추적 파일만, `.env*` 제외 규칙, 로그 마스킹 |
| 출력 | 봇 응답에 비밀값 패턴 감지 시 마스킹 |
| 프롬프트 인젝션 | 문서·코드 내용은 데이터로만 취급, 봇에 쓰기·외부 전송 권한 없음 |
| 서버 | SSH는 키 인증 + IP 제한, 나머지 인바운드 차단 |
| 감사 | 모든 질의·도구 호출·인용을 append-only로 기록 |

## 8. 비용 장치

| 장치 | 내용 |
| --- | --- |
| 되묻기 | 넓은 질문은 탐색 전에 반문 |
| 도구 결과 상한 | 상위 k개, 구간 읽기만 |
| 프롬프트 캐싱 | 고정 앞부분 캐시 |
| 모델 배분 | 소형: 스코핑·라우팅·단순 답변 / 대형: 에이전트 최종 추론 |
| 예산 | 질문 유형별 토큰 예산, 일일 한도 |
| 기록 | 질문별 입력·출력·캐시 토큰과 비용을 감사 로그에 저장 |

## 9. 평가

### 9.1 골든 세트

`eval/golden.yaml`에 law-watch 기준 질문 25개를 여러 도메인에 고르게 작성한다. 내가 만든 코드라 정답과 근거를 정확히 채울 수 있다.

```yaml
- id: q01
  question: 앱에서 의안 상세 화면은 네이티브야 웹뷰야?
  type: simple
  expected_domains: [bills, mobile-hybrid]
  answer: embedded(WebView)로 웹 상세를 띄운다
  evidence:
    - apps/mobile/src/navigation/public-route-manifest.ts
  expected_behavior: answer
- id: q20
  question: 알림 로직 알려줘
  type: broad
  expected_behavior: clarify
```

유형별 최소 개수: 단순 6, 정책 5, 화면·하이브리드 5, 다중 홉 4, 결정 배경 3, 경계 케이스(되묻기·설정 의존·문서 불일치·범위 밖) 각 1 이상.

### 9.2 지표와 하네스

| 단계 | 지표 | 실패 시 고칠 곳 |
| --- | --- | --- |
| 스코핑 | 범위 재현율 | 도메인 맵, 용어 사전 |
| 검색 | 근거 재현율 (Recall@k) | 인덱스, 도구 |
| 생성 | 정답률(LLM 판정 + 수동 확인), 인용 정확도 | 프롬프트, 도구 설계 |
| 비용 | 질문당 토큰·비용·지연 | 라우팅, 상한 |

`python eval/run_eval.py`가 슬랙을 거치지 않고 파이프라인을 직접 호출해 결과표(마크다운)를 만든다.

## 10. 노션 데모 문서

law-watch 실제 기능을 바탕으로 기획자 관점 문서 8~10개를 노션에 작성한다. 모든 페이지 상단에 "AskCode PoC를 위해 작성한 문서"임을 표시한다.

| 페이지 | 바탕 | 용도 |
| --- | --- | --- |
| 내 소식 푸시 정책서 | `docs/plans/my-news-push-policy.md`, 푸시 코드 | 정책 질문, 코드 대조 |
| 팔로우 기능 기획서 | 팔로우·게스트 병합 로직 | 다중 홉 추적 |
| 의안 상세 화면 기획 | 앱 WebView 상세, 웹 상세 | 하이브리드 질문 |
| 모바일 하이브리드 운영 원칙 | `docs/plans/mobile-webview-first-plan.md` 결정 | 결정 배경 |
| 검색 기획서 | PostgreSQL 전문 검색 | 단순 질문 |
| AI 탐색 기능 기획서 | `apps/api/src/ai-exploration` | 정책·제약 |
| 회의록 2~3건 | 실제 결정 이력 | 결정 배경 |
| 0.2.7 출시 체크리스트 | 최근 릴리스 | 기준 버전 |

의도적 불일치 2~3개를 포함하고 목록을 `eval/golden.yaml`의 경계 케이스와 연결한다.

## 11. 일정 (4일)

### 1일차 — 기반과 코드 Q&A

- [ ] 프로젝트 골격, 설정, SQLite 스키마, 감사 로그
- [ ] GitHub App 생성·설치, 레포 동기화(얕은 클론 + fetch)
- [ ] 제외 규칙 적용 코드 인덱스(파일·심볼)
- [ ] 슬랙 앱 생성, Socket Mode 연결, "확인 중…" → 응답 갱신
- [ ] Claude Agent SDK 연결, 읽기 전용 기본 도구(code_search, read_code)
- [ ] 골든 세트 25개 초안
- 완료 기준: 슬랙에서 단순 질문에 `파일:라인` 인용 답변, 질문당 비용 기록

### 2일차 — 하이브리드 추적과 스코핑

- [ ] API 연결 맵, 화면·라우트 맵, WebView 브리지 맵
- [ ] `trace_screen`, `trace_api`, `read_route_manifest` 도구
- [ ] 도메인 맵 YAML, 용어 사전 추출
- [ ] 스코핑, 되묻기 버튼, 라우팅, 범위 하드 필터
- [ ] 인용 검증, 확신도, 기준 커밋 표시
- 완료 기준: 하이브리드 다중 홉 질문 성공, 넓은 질문에 되묻기

### 3일차 — 문서 소스와 평가

- [ ] 노션 데모 문서 작성, 노션 통합 연결, 문서 인덱스(레포 docs + 노션)
- [ ] 소스 충돌 병기, 설정 의존 표시
- [ ] (선택) 슬랙 지정 채널 인덱스
- [ ] 평가 하네스 실행, 결과표 작성, 낮은 항목 수선 후 재실행
- [ ] (시간 여유 시) 스크린샷 역추적
- 완료 기준: 골든 세트 정답률·인용 정확도·질문당 비용 수치 확보

### 4일차 — 배포와 데모 준비

- [ ] Vultr 서울 인스턴스, Docker Compose 배포, 방화벽·비밀값 설정
- [ ] 데모 질문 사전 실행으로 캐시 예열
- [ ] 시나리오 리허설 2회, 백업 영상 녹화
- [ ] README, 결과표, "이 회사에 적용한다면" 문서 정리

## 12. 데모 시나리오 (약 7분)

1. 문제 제기 (30초): 기획자의 반복 질문이 개발 시간을 잠식한다
2. 단순 질문 (30초): "검색은 어떤 방식으로 동작해?"
3. 하이브리드 추적 (2분): "앱 의안 상세에서 팔로우 누르면 어떻게 처리돼?" — 네이티브·WebView·API·서버 단계별 인용과 레이어 표시
4. 되묻기 (30초): "알림 로직 알려줘" → 후보 버튼
5. 함정 (1분 30초): 플랫폼 차이(웹 페이지네이션 vs 앱 무한스크롤), 노션 기획서와 코드 불일치, 설정 의존 값
6. 평가와 비용 (1분): 골든 세트 결과표, 질문당 비용, 되묻기로 줄인 토큰
7. 적용 방안 (1분): 하이브리드 구조 대응, 규제 환경이면 LLM 포트를 클라우드 내 모델로 교체, 파일럿 계획

## 13. 리스크와 대응

| 리스크 | 대응 |
| --- | --- |
| 에이전트 응답 지연으로 라이브 데모 끊김 | 데모 질문 캐시 예열, 단순 질문은 라우팅으로 빠르게, 백업 영상 |
| 레포 규모로 탐색 과다 | 스코핑 하드 필터, 도구 결과 상한, 고수준 추적 도구 |
| main에 미배포 코드 포함 | 답변에 기준 커밋 표시, 최근 변경 파일 안내 |
| 문서가 지나치게 잘 정리된 레포라는 반론 | 문서 소스를 끈 상태의 정답률도 함께 측정해 제시 |
| 로컬과 원격 커밋 차이 | 데모 전 law-watch 최신 커밋 푸시 |
| 일정 지연 | 우선순위: 1·2일차 > 평가 > 노션 > 슬랙 소스 > 스크린샷 |

## 14. 사전 준비 체크리스트

- [ ] Claude API 키
- [ ] 슬랙 워크스페이스와 앱 생성 권한 (Socket Mode, Bot Token, App Token)
- [ ] GitHub App 생성, law-watch 레포 설치
- [ ] 노션 워크스페이스, 내부 통합 생성
- [ ] Vultr 계정, 서울 리전 인스턴스
- [ ] law-watch 로컬 최신 커밋 푸시
