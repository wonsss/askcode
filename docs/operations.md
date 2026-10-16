# DB 운영 런북

AskCode의 데이터는 SQLite 파일 하나(`data/askcode.sqlite3`)에 있다. 이 문서는 배포 후 그 파일을 어떻게 지키고, 정리하고, 문제가 생기면 어떻게 되돌리는지 정리한다. 여기 나오는 명령은 모두 Claude API를 쓰지 않는다(비용 0).

## 무엇이 어디에 있나

| 데이터 | 위치 | 잃으면 |
| --- | --- | --- |
| **감사 로그**: 질문·답·근거·탐색 과정·피드백·비용·가림 기록 | `data/askcode.sqlite3` (Docker: 볼륨 `askcode-data`) | **복구 불가.** 백업이 유일한 사본 |
| 코드·문서 인덱스, 구조 맵, 커밋별 파일 해시 | 같은 파일 | `askcode sync`로 다시 만든다. 단, 예전 커밋 해시는 사라져 옛 답의 최신성 판단이 ❔로 바뀐다 |
| law-watch 클론 | `data/repos/` | `askcode sync`로 다시 받는다 |
| 백업 | `backups/` (Docker: 호스트 `./backups`) + 서버 밖 복사본 | 서버 밖 복사본이 있으면 괜찮다 |

`data/`, `backups/`는 git에 올리지 않는다(`.gitignore`). 질문에 비공개 코드 정보가 들어 있기 때문이다.

## 보존 정책

**감사 로그는 영구 보존한다.** (2026-10-16 결정)

- 질문·답·근거·피드백 행은 지우지 않는다. 테이블 트리거가 수정·삭제를 막는다(append-only).
- 로컬 백업 파일만 `ASKCODE_BACKUP_KEEP_DAYS`(기본 14일)가 지나면 지운다. 서버 밖 복사본의 보관 기간은 저장소의 수명 주기 설정으로 정한다(권장: 90일 이상).
- 정리 대상은 감사 로그가 아니다. 답변이 참조하지 않는 오래된 **커밋 스냅숏**과 WAL 파일만 정리한다.
- 개인정보가 섞여 들어온 경우에만 **가리기**(`askcode redact`)를 쓴다. 행은 남기고 텍스트만 가린다.

정책을 바꿀 때(예: 1년 뒤 질문 문장 익명화)는 이 절과 `askcode maintenance`를 함께 고친다.

## 매일 자동으로 하는 일 (Docker `backup` 서비스 또는 맥북 호스트 모드)

맥북 호스트 모드(`askcode host on`)에서는 launchd가 매일 03:30에 같은 순서로 실행하고, 로그는 `data/logs/backup.log`에 남는다. 맥북이 그 시각에 꺼져 있으면 건너뛴다.

```
askcode backup && askcode restore-check && askcode maintenance
```

| 단계 | 하는 일 |
| --- | --- |
| `backup` | SQLite 온라인 백업(봇이 쓰는 중에도 일관된 스냅숏) → 무결성 확인 → `.sha256` 기록 → 14일 지난 로컬 백업 정리 → `ASKCODE_BACKUP_UPLOAD_CMD`로 서버 밖 복사 |
| `restore-check` | 방금 만든 백업을 읽기 전용으로 열어 무결성·스키마 버전·질문 수 확인 |
| `maintenance` | 답변이 참조하지 않는 커밋 스냅숏 삭제, WAL 체크포인트, 통계 갱신 |

하나라도 실패하면 `backup failed`를 남긴다(`docker compose logs backup`).

### 서버 밖 복사 설정

백업이 같은 서버 디스크에만 있으면 서버가 사라질 때 함께 사라진다. 배포할 때 반드시 설정한다.

```
# .env 예시 — {file}이 백업 파일 경로로 바뀐다. 셸을 거치지 않으므로 파이프·변수 확장은 쓸 수 없다.
ASKCODE_BACKUP_UPLOAD_CMD=rclone copy {file} r2:askcode-backups
# 또는
ASKCODE_BACKUP_UPLOAD_CMD=aws s3 cp {file} s3://askcode-backups/
```

업로드 도구(rclone, aws)는 백업 컨테이너 이미지에 설치해야 한다. 저장소 쪽 자격 증명은 그 도구의 설정으로 넣고, AskCode `.env`에는 넣지 않는다.

## 점검 (`askcode doctor`)

| 항목 | 기준 |
| --- | --- |
| DB 무결성 | `quick_check`가 `ok` |
| DB 스키마 | 코드의 `SCHEMA_VERSION`과 같음 |
| DB 용량 | 디스크 여유 2GB 이상 |
| 최근 백업 | 36시간 이내, 서버 밖 복사 설정됨 |

주 1회 `doctor`를 돌리거나, 배포 환경의 모니터링에서 `backups/`의 최신 파일 시각을 본다.

## 복구 절차

1. 백업을 고른다. 로컬이면 `backups/`, 서버가 없어졌으면 서버 밖 복사본을 내려받는다.
2. 복구 전에 확인한다.
   ```bash
   uv run askcode restore-check backups/askcode-20261016-001542.sqlite3
   sha256sum -c backups/askcode-20261016-001542.sqlite3.sha256
   ```
3. 봇을 멈춘다(`docker compose stop askcode` 또는 `Ctrl+C`).
4. 현재 DB를 옆으로 옮기고, 백업을 제자리에 둔다. WAL 파일도 함께 옮긴다.
   ```bash
   mv data/askcode.sqlite3 data/askcode.sqlite3.broken
   mv data/askcode.sqlite3-wal data/askcode.sqlite3-shm /tmp/ 2>/dev/null
   cp backups/askcode-20261016-001542.sqlite3 data/askcode.sqlite3
   ```
5. 봇을 시작하고 `askcode doctor`로 확인한다. 백업 이후 질문은 사라진 상태다.
6. 인덱스는 백업에 들어 있으므로 바로 동작한다. 최신 코드로 맞추려면 `askcode sync`.

**연습:** 파일럿 시작 전에 한 번, 이후 분기마다 한 번 다른 폴더에서 4~5단계를 해 본다.

## 확인 필요 처리 (`askcode resolve`)

콘솔·메뉴 막대의 "확인 필요"는 최근 30일 슬랙 질문 중 확신도 낮음·👎·확인 요청·오류·한도 도달·범위 미스가 있는 건이다. 살펴보고 조치했으면 처리됨으로 표시해 목록과 숫자에서 뺀다.

```bash
uv run askcode resolve <query_id> --note "답 맞음, 질문자에게 회신"
uv run askcode resolve <id1> <id2> --note "크레딧 부족 때 오류, 재질문 안내"
uv run askcode resolve --all --note "10-16 일괄 점검"   # 지금 목록 전부
uv run askcode resolve <query_id> --reopen              # 되돌리기
```

- 기록은 `resolutions` 테이블에 **추가만** 한다(append-only). 질문별 마지막 기록이 상태다.
- 처리한 뒤 새 👎나 확인 요청이 들어오면 자동으로 다시 열린다.
- 콘솔 확인 필요 화면의 "처리된 건 보기"에서 누가·언제·어떤 메모로 처리했는지 본다. 질문 상세에 처리 명령이 그대로 적혀 있다.
- 콘솔은 읽기 전용이라 처리 버튼이 없다. 처리는 운영자가 CLI로 한다.

## 개인정보가 들어왔을 때 (`askcode redact`)

```bash
uv run askcode redact <query_id> --reason "질문에 실명·연락처 포함"
```

- 질문 문장, 답변, 탐색 과정의 도구 입력을 `[삭제됨]`으로 바꾼다. 근거 경로·비용·피드백 행은 남는다.
- 누가, 언제, 왜 가렸는지 `redactions` 테이블에 남고, 이 기록도 지울 수 없다.
- 트리거는 쓰기 잠금을 건 한 트랜잭션 안에서만 잠시 내렸다가 다시 건다. 도중에 실패하면 전부 되돌린다.
- `query_id`는 웹 콘솔 질문 상세의 고정 링크(`/q/<id>`)에서 찾는다.
- **백업에는 가리기 전 내용이 남는다.** 완전히 지워야 하면 그 기간의 로컬·서버 밖 백업도 확인한다.
- 슬랙 원본 메시지는 별도다. 슬랙에서 지운다.

## 스키마 변경

- DB에는 스키마 버전(`PRAGMA user_version`)이 있다. 현재 v4(v1 기준 테이블 + v2 가림 기록 + v3 `query_results.provider` 컬럼 — 답변에 쓰인 인증 경로 api|subscription + v4 처리 기록 `resolutions`).
- 테이블을 바꿀 때는 `src/askcode/store/db.py`의 `MIGRATIONS`에 번호를 붙여 **추가만** 한다. 이미 배포한 항목은 고치지 않는다.
- 봇·콘솔·명령이 시작할 때 자기 버전보다 큰 마이그레이션만 순서대로 적용한다.
- 바꾸기 전에 `askcode backup`을 먼저 한다.

## 다른 맥으로 이전

봇의 몸통은 레포+`.env`+DB+launchd이고, 메뉴바 앱은 조종판이라 따로 옮기지 않는다. 새 맥에서는 이렇게 세팅한다.

1. **준비물을 옮긴다** — 클라우드 동기화가 아니라 `scp`/AirDrop 등으로 직접 보낸다(비밀값 포함).
   - 레포 클론(`git clone`), `uv`(brew)
   - `.env` 전체, `secrets/github-app.pem`
   - `data/askcode.sqlite3`(답변 이력을 살릴 때만 — 안 옮기면 `askcode sync`로 새로 시작)
   - 터널: `~/.cloudflared/<tunnel-id>.json` + `config.yml` (로그인 없이 자격증명 파일만 있으면 `tunnel run`이 동작한다. 양쪽 맥에서 동시에 띄워도 되고, 되면 Cloudflare가 커넥터 둘에 분산한다)
2. **새 맥에서**
   ```bash
   uv sync && uv run askcode doctor --online
   uv run askcode sync                    # DB를 안 옮겼을 때
   uv run askcode host on                 # 봇·콘솔·백업 launchd 등록
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.askcode.tunnel.plist   # plist도 함께 옮겼다면
   bash scripts/build-macos-app.sh        # 이 맥 경로가 박힌 앱으로 새로 빌드·설치
   ```
3. **구독 모드를 쓸 거면** 새 맥에서 `claude auth login`으로 로그인해야 한다(OAuth 자격증명은 맥 로컬 키체인에 있다). API 모드는 `.env`만 옮기면 끝.
4. **주의: 봇은 한 대에서만 켠다.** `serve.lock`은 각 맥의 로컬 파일이라 두 맥이 동시에 봇을 띄우면 슬랙이 이벤트를 양쪽에 나눠 줘 답이 엇갈리고 감사 로그가 갈라진다. 옮긴 뒤 예전 맥은 `askcode host off`로 끈다.

## 웹 콘솔 외부 공개 (Cloudflare Tunnel + Access)

콘솔은 여전히 `127.0.0.1:8787`에만 바인딩하고, 맥북의 `cloudflared`가 나가는 연결로 Cloudflare에 붙는다. Cloudflare Access가 터널 앞에서 이메일 인증을 거므로 허용된 사람만 열린다.

| 항목 | 값 |
| --- | --- |
| 공개 주소 | `https://askcode.lawwatch.kr` |
| 터널 | `askcode-console` (`8b8db7fe-…`, 설정 `~/.cloudflared/config.yml`, 자격증명 `~/.cloudflared/*.json`) |
| 상시 실행 | launchd `com.askcode.tunnel` (`~/Library/LaunchAgents/`), 로그 `data/logs/tunnel.log` |
| 인증 | Zero Trust → Access controls → Applications의 `AskCode 콘솔` 앱, Allow 정책(이메일 목록) |
| 팀원 추가 | 그 정책의 Emails에 주소를 추가하면 됨(코드 변경 없음) |
| 끄기 | `launchctl bootout gui/$(id -u)/com.askcode.tunnel` + plist 삭제. 완전 철거는 `cloudflared tunnel delete askcode-console` + Access 앱 삭제 |

## 언제 SQLite를 벗어나나

서버 1대, 팀 하나, 하루 수십 건인 파일럿에는 SQLite로 충분하다. 쓰는 곳은 봇 하나이고 콘솔은 읽기만 한다. 아래 중 하나가 생기면 PostgreSQL을 검토한다.

- 봇을 여러 대 띄워야 할 때
- 여러 팀이 같은 DB를 쓸 때
- 관리형 백업·시점 복구·접근 권한 관리가 필요할 때

## 크기 예상

- 감사 로그: 질문 1건에 수십 KB(답변·탐색 과정·근거). 하루 30건이면 월 수 MB.
- 커밋 스냅숏: 코드가 바뀐 동기화마다 약 3,600행. `maintenance`가 답변이 참조하지 않는 커밋을 지운다.
- 2026-10-16 기준: DB 21MB(질문 115건, 인덱스 포함), 클론 84MB.
