import os
import sqlite3
import time

import pytest

from askcode.ops import backup, latest_backup, maintenance, verify_backup
from askcode.store.db import REDACTED, SCHEMA_VERSION, QueryResult, Store


def _seed(st: Store, commit: str = "c1") -> str:
    qid = st.log_query("홍길동 010-1234-5678 푸시 시각?", "slack", "U1", "D1", "1.0", commit)
    st.log_result(qid, QueryResult(status="ok", answer="09:00이에요", confidence="high", route="agent",
                                   scope='{"domains": ["follows-push"]}'))
    st.log_tool_call(qid, "code_search", {"query": "홍길동"}, "1건")
    st.log_citation(qid, "code", "a/b.ts", 1, 2, True, "ok")
    return qid


def test_backup_verify_prune_and_upload(tmp_path):
    st = Store(tmp_path / "db.sqlite3")
    _seed(st)
    dest = tmp_path / "backups"
    dest.mkdir()
    old = dest / "askcode-20200101-000000.sqlite3"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    remote = tmp_path / "remote"
    remote.mkdir()
    r = backup(tmp_path / "db.sqlite3", dest, keep_days=14, upload_cmd=f"cp {{file}} {remote}")
    assert r.queries == 1 and r.uploaded and old in r.pruned and not old.exists()
    assert (remote / r.path.name).exists()  # 서버 밖 복사
    assert sorted(p.name for p in dest.iterdir()) == [r.path.name, r.path.name + ".sha256"]  # -wal·-shm 없음
    assert latest_backup(dest) == r.path
    v = verify_backup(r.path)
    assert v["integrity"] == "ok" and v["queries"] == 1 and v["schema_version"] == SCHEMA_VERSION


def test_backup_upload_failure_is_reported(tmp_path):
    st = Store(tmp_path / "db.sqlite3")
    _seed(st)
    with pytest.raises(RuntimeError, match="찾을 수 없습니다"):
        backup(tmp_path / "db.sqlite3", tmp_path / "b", upload_cmd="no-such-uploader {file}")
    with pytest.raises(RuntimeError, match="서버 밖 복사 실패"):
        backup(tmp_path / "db.sqlite3", tmp_path / "b", upload_cmd="cp {file} /nonexistent/dir/")


def test_maintenance_drops_only_unreferenced_snapshots(tmp_path):
    st = Store(tmp_path / "db.sqlite3")
    _seed(st, commit="c1")
    st.set_repo_state("law-watch", "c3")
    for sha in ("c1", "c2", "c3"):
        st.record_commit_files("law-watch", sha, [("a/b.ts", f"blob-{sha}")])
    conn = sqlite3.connect(tmp_path / "db.sqlite3", isolation_level=None)
    r = maintenance(conn, "law-watch")
    left = {x[0] for x in conn.execute("SELECT DISTINCT commit_sha FROM commit_files")}
    assert left == {"c1", "c3"} and r["snapshots_dropped"] == 1  # 답변이 쓰는 c1, 현재 c3는 남김
    assert conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 1  # 감사 로그는 그대로


def test_redact_hides_text_keeps_rows_and_trigger(tmp_path):
    st = Store(tmp_path / "db.sqlite3")
    qid = _seed(st)
    assert st.redact(qid, "marco", "개인정보 유입")
    q = st.execute("SELECT question FROM queries WHERE id=?", (qid,)).fetchone()[0]
    a = st.execute("SELECT answer, scope FROM query_results WHERE query_id=?", (qid,)).fetchone()
    t = st.execute("SELECT input_json FROM tool_calls WHERE query_id=?", (qid,)).fetchone()[0]
    assert q == REDACTED and a[0] == REDACTED and a[1] is None and t == "{}"
    assert st.execute("SELECT COUNT(*) FROM citations WHERE query_id=?", (qid,)).fetchone()[0] == 1  # 행은 남김
    rec = st.execute("SELECT operator, reason FROM redactions WHERE query_id=?", (qid,)).fetchone()
    assert tuple(rec) == ("marco", "개인정보 유입")
    with pytest.raises(sqlite3.IntegrityError):  # 트리거가 다시 걸려 있다
        st.execute("UPDATE queries SET question='x' WHERE id=?", (qid,))
    with pytest.raises(sqlite3.IntegrityError):  # 가림 기록도 지울 수 없다
        st.execute("DELETE FROM redactions")
    assert not st.redact("nope", "marco", "x")
    with pytest.raises(ValueError):
        st.redact(qid, "marco", " ")


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "db.sqlite3"
    Store(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE redactions")
    conn.execute("PRAGMA user_version = 0")  # 버전 관리 이전에 만든 DB
    conn.commit()
    conn.close()
    st = Store(path)
    assert st.schema_version == SCHEMA_VERSION
    assert st.execute("SELECT name FROM sqlite_master WHERE name='redactions'").fetchone()
