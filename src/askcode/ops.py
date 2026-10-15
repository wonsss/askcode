"""DB 운영: 백업·복구 확인·정리. 보존 정책은 '감사 로그 영구 보존'이다(docs/operations.md).

Claude API를 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

BACKUP_PREFIX = "askcode-"
BACKUP_SUFFIX = ".sqlite3"


@dataclass
class BackupResult:
    path: Path
    bytes: int
    sha256: str
    queries: int
    pruned: list[Path]
    uploaded: bool | None  # None = 업로드 설정 없음


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_backup(path: Path) -> dict:
    """백업 파일을 읽기 전용으로 열어 무결성과 내용을 확인한다(복구 연습)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        n, last = conn.execute("SELECT COUNT(*), MAX(ts) FROM queries").fetchone()
        results = conn.execute("SELECT COUNT(*) FROM query_results").fetchone()[0]
    finally:
        conn.close()
    return {"integrity": integrity, "schema_version": version, "queries": n, "results": results,
            "latest": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "-"}


def backup(db_path: Path, dest: Path, keep_days: int = 14, upload_cmd: str | None = None) -> BackupResult:
    """SQLite 온라인 백업 → 무결성 확인 → sha256 기록 → 오래된 로컬 백업 정리 → (설정 시) 서버 밖으로 복사.

    upload_cmd 예: "rclone copy {file} r2:askcode-backups" — {file} 자리에 백업 파일 경로가 들어간다.
    셸을 거치지 않고 실행한다(쉘 확장·파이프 없음).
    """
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{BACKUP_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}{BACKUP_SUFFIX}"
    tmp = out.with_suffix(".partial")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)  # WAL 포함 일관된 스냅숏, 봇이 쓰는 중에도 안전
        dst.execute("PRAGMA journal_mode=DELETE")  # 백업본은 -wal·-shm 없는 단일 파일로
    finally:
        dst.close()
        src.close()
    check = verify_backup(tmp)
    if check["integrity"] != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"백업 무결성 확인 실패: {check['integrity']}")
    tmp.rename(out)
    digest = _sha256(out)
    out.with_suffix(out.suffix + ".sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")

    pruned = []
    cutoff = time.time() - keep_days * 86400
    for old in sorted(dest.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")):
        if old != out and old.stat().st_mtime < cutoff:
            old.unlink()
            Path(str(old) + ".sha256").unlink(missing_ok=True)
            pruned.append(old)

    uploaded = None
    if upload_cmd:
        args = [a.replace("{file}", str(out)) for a in shlex.split(upload_cmd)]
        if shutil.which(args[0]) is None:
            raise RuntimeError(f"업로드 명령을 찾을 수 없습니다: {args[0]}")
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"서버 밖 복사 실패({r.returncode}): {r.stderr.strip()[:300]}")
        uploaded = True
    return BackupResult(out, out.stat().st_size, digest, check["queries"], pruned, uploaded)


def latest_backup(dest: Path) -> Path | None:
    files = sorted(dest.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def maintenance(conn: sqlite3.Connection, repo: str, vacuum: bool = False) -> dict:
    """정리: 답변이 참조하지 않는 오래된 커밋 스냅숏 삭제, WAL 체크포인트, 통계 갱신, (선택) VACUUM.

    감사 로그(질문·답·근거·피드백)는 건드리지 않는다(영구 보존).
    """
    current = conn.execute("SELECT commit_sha FROM repo_state WHERE repo=?", (repo,)).fetchone()
    keep = {r[0] for r in conn.execute("SELECT DISTINCT commit_sha FROM queries WHERE commit_sha IS NOT NULL")}
    if current:
        keep.add(current[0])
    snapshots = {r[0] for r in conn.execute("SELECT DISTINCT commit_sha FROM commit_files WHERE repo=?", (repo,))}
    drop = sorted(snapshots - keep)
    removed = 0
    for sha in drop:
        removed += conn.execute("DELETE FROM commit_files WHERE repo=? AND commit_sha=?", (repo, sha)).rowcount
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA optimize")
    if vacuum:
        conn.execute("VACUUM")
    return {"snapshots_kept": len(snapshots) - len(drop), "snapshots_dropped": len(drop), "rows_dropped": removed,
            "vacuumed": vacuum}
