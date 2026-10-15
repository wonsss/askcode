"""SQLite 저장소. 감사 로그 테이블은 트리거로 수정·삭제를 막아 append-only로 유지한다."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS repo_state (
  repo TEXT PRIMARY KEY,
  commit_sha TEXT NOT NULL,
  synced_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  repo TEXT NOT NULL,
  path TEXT NOT NULL,
  lang TEXT,
  lines INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  PRIMARY KEY (repo, path)
);

CREATE TABLE IF NOT EXISTS structure (
  repo TEXT NOT NULL,
  kind TEXT NOT NULL,            -- api | route | manifest | bridge
  key TEXT NOT NULL,
  label TEXT,
  path TEXT NOT NULL,
  line INTEGER NOT NULL,
  extra TEXT
);
CREATE INDEX IF NOT EXISTS structure_kind ON structure(repo, kind);

-- 문서 청크 (레포 docs, 노션, 슬랙). path는 레포 경로 또는 notion/…, slack/… 가상 경로
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,          -- repo | notion | slack
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  heading TEXT,
  url TEXT,
  updated_at TEXT,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_path ON docs(path);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(title, heading, text, content='docs', content_rowid='id', tokenize='trigram');
CREATE TABLE IF NOT EXISTS doc_files (
  path TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT,
  updated_at TEXT,
  lines INTEGER NOT NULL,
  body TEXT NOT NULL
);

-- 감사 로그 (append-only)
CREATE TABLE IF NOT EXISTS queries (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  source TEXT NOT NULL,          -- slack | cli | eval
  user_id TEXT,
  channel TEXT,
  thread_ts TEXT,
  question TEXT NOT NULL,
  commit_sha TEXT
);

CREATE TABLE IF NOT EXISTS query_results (
  query_id TEXT PRIMARY KEY REFERENCES queries(id),
  ts REAL NOT NULL,
  status TEXT NOT NULL,          -- ok | error | clarify
  answer TEXT,
  confidence TEXT,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  cost_usd REAL,
  duration_ms INTEGER,
  num_turns INTEGER,
  error TEXT,
  scope TEXT,                    -- 스코핑 결과 JSON
  route TEXT                     -- simple | agent | clarify | refuse
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query_id TEXT NOT NULL REFERENCES queries(id),
  ts REAL NOT NULL,
  tool TEXT NOT NULL,
  input_json TEXT NOT NULL,
  result_summary TEXT
);

CREATE TABLE IF NOT EXISTS citations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query_id TEXT NOT NULL REFERENCES queries(id),
  source TEXT NOT NULL,          -- code | doc | notion | slack
  path TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER,
  verified INTEGER NOT NULL,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query_id TEXT NOT NULL REFERENCES queries(id),
  ts REAL NOT NULL,
  user_id TEXT,
  value TEXT NOT NULL             -- up | down | escalate
);
"""

APPEND_ONLY_TABLES = ("queries", "query_results", "tool_calls", "citations", "feedback")


def _append_only_triggers() -> str:
    parts = []
    for table in APPEND_ONLY_TABLES:
        for op in ("UPDATE", "DELETE"):
            parts.append(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()}
BEFORE {op} ON {table}
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;"""
            )
    return "\n".join(parts)


@dataclass
class QueryResult:
    status: str
    answer: str | None = None
    confidence: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    error: str | None = None
    scope: str | None = None
    route: str | None = None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(SCHEMA)
        self._conn.executescript(_append_only_triggers())

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # 레포·파일 인덱스 (갱신 가능)
    def set_repo_state(self, repo: str, commit_sha: str) -> None:
        self.execute(
            "INSERT INTO repo_state(repo, commit_sha, synced_at) VALUES (?,?,?) "
            "ON CONFLICT(repo) DO UPDATE SET commit_sha=excluded.commit_sha, synced_at=excluded.synced_at",
            (repo, commit_sha, time.time()),
        )

    def get_repo_commit(self, repo: str) -> str | None:
        row = self.execute("SELECT commit_sha FROM repo_state WHERE repo=?", (repo,)).fetchone()
        return row["commit_sha"] if row else None

    def replace_files(self, repo: str, rows: list[tuple[str, str | None, int, int]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM files WHERE repo=?", (repo,))
                self._conn.executemany(
                    "INSERT INTO files(repo, path, lang, lines, bytes) VALUES (?,?,?,?,?)",
                    [(repo, *r) for r in rows],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def file_info(self, repo: str, path: str) -> sqlite3.Row | None:
        return self.execute("SELECT * FROM files WHERE repo=? AND path=?", (repo, path)).fetchone()

    def file_count(self, repo: str) -> int:
        return self.execute("SELECT COUNT(*) AS n FROM files WHERE repo=?", (repo,)).fetchone()["n"]

    # 구조 맵
    def replace_structure(self, repo: str, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM structure WHERE repo=?", (repo,))
                self._conn.executemany(
                    "INSERT INTO structure(repo, kind, key, label, path, line, extra) VALUES (?,?,?,?,?,?,?)",
                    [(repo, *r) for r in rows],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # 문서
    def replace_docs(self, source: str, files: list[dict], chunks: list[dict]) -> None:
        """source 단위로 문서를 통째로 교체한다. files: path,title,url,updated_at,lines,body / chunks: path,title,heading,url,updated_at,start_line,end_line,text"""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                ids = [r[0] for r in self._conn.execute("SELECT id FROM docs WHERE source=?", (source,))]
                for i in ids:
                    row = self._conn.execute("SELECT title, heading, text FROM docs WHERE id=?", (i,)).fetchone()
                    self._conn.execute("INSERT INTO docs_fts(docs_fts, rowid, title, heading, text) VALUES('delete',?,?,?,?)",
                                       (i, row[0], row[1], row[2]))
                self._conn.execute("DELETE FROM docs WHERE source=?", (source,))
                self._conn.execute("DELETE FROM doc_files WHERE source=?", (source,))
                for f in files:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO doc_files(path, source, title, url, updated_at, lines, body) VALUES (?,?,?,?,?,?,?)",
                        (f["path"], source, f["title"], f.get("url"), f.get("updated_at"), f["lines"], f["body"]),
                    )
                for c in chunks:
                    cur = self._conn.execute(
                        "INSERT INTO docs(source, path, title, heading, url, updated_at, start_line, end_line, text) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (source, c["path"], c["title"], c.get("heading"), c.get("url"), c.get("updated_at"),
                         c["start_line"], c["end_line"], c["text"]),
                    )
                    self._conn.execute("INSERT INTO docs_fts(rowid, title, heading, text) VALUES (?,?,?,?)",
                                       (cur.lastrowid, c["title"], c.get("heading") or "", c["text"]))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def doc_file(self, path: str) -> sqlite3.Row | None:
        return self.execute("SELECT * FROM doc_files WHERE path=?", (path,)).fetchone()

    # 감사 로그 (append-only)
    def log_query(
        self,
        question: str,
        source: str,
        user_id: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        commit_sha: str | None = None,
    ) -> str:
        query_id = uuid.uuid4().hex
        self.execute(
            "INSERT INTO queries(id, ts, source, user_id, channel, thread_ts, question, commit_sha) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (query_id, time.time(), source, user_id, channel, thread_ts, question, commit_sha),
        )
        return query_id

    def log_result(self, query_id: str, r: QueryResult) -> None:
        self.execute(
            "INSERT INTO query_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                query_id, time.time(), r.status, r.answer, r.confidence, r.model,
                r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens,
                r.cost_usd, r.duration_ms, r.num_turns, r.error, r.scope, r.route,
            ),
        )

    def log_tool_call(self, query_id: str, tool: str, tool_input: dict[str, Any], summary: str | None) -> None:
        self.execute(
            "INSERT INTO tool_calls(query_id, ts, tool, input_json, result_summary) VALUES (?,?,?,?,?)",
            (query_id, time.time(), tool, json.dumps(tool_input, ensure_ascii=False), summary),
        )

    def log_citation(
        self, query_id: str, source: str, path: str, start: int | None, end: int | None, verified: bool, reason: str
    ) -> None:
        self.execute(
            "INSERT INTO citations(query_id, source, path, start_line, end_line, verified, reason) "
            "VALUES (?,?,?,?,?,?,?)",
            (query_id, source, path, start, end, int(verified), reason),
        )

    def log_feedback(self, query_id: str, user_id: str | None, value: str) -> None:
        self.execute(
            "INSERT INTO feedback(query_id, ts, user_id, value) VALUES (?,?,?,?)",
            (query_id, time.time(), user_id, value),
        )
