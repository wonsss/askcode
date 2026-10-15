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

- DB에는 스키마 버전(`PRAGMA user_version`)이 있다. 현재 v2(v1 기준 테이블 + v2 가림 기록).
- 테이블을 바꿀 때는 `src/askcode/store/db.py`의 `MIGRATIONS`에 번호를 붙여 **추가만** 한다. 이미 배포한 항목은 고치지 않는다.
- 봇·콘솔·명령이 시작할 때 자기 버전보다 큰 마이그레이션만 순서대로 적용한다.
- 바꾸기 전에 `askcode backup`을 먼저 한다.

## 언제 SQLite를 벗어나나

서버 1대, 팀 하나, 하루 수십 건인 파일럿에는 SQLite로 충분하다. 쓰는 곳은 봇 하나이고 콘솔은 읽기만 한다. 아래 중 하나가 생기면 PostgreSQL을 검토한다.

- 봇을 여러 대 띄워야 할 때
- 여러 팀이 같은 DB를 쓸 때
- 관리형 백업·시점 복구·접근 권한 관리가 필요할 때

## 크기 예상

- 감사 로그: 질문 1건에 수십 KB(답변·탐색 과정·근거). 하루 30건이면 월 수 MB.
- 커밋 스냅숏: 코드가 바뀐 동기화마다 약 3,600행. `maintenance`가 답변이 참조하지 않는 커밋을 지운다.
- 2026-10-16 기준: DB 21MB(질문 115건, 인덱스 포함), 클론 84MB.
