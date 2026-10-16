"""웹 콘솔 데이터 계층. 감사 로그 SQLite를 읽기 전용(mode=ro)으로 연다.

Claude API를 호출하지 않는다. 답변 파싱은 슬랙과 같은 함수를 재사용해 같은 결과를 보여 준다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from askcode.pipeline.answer import LIMIT_ERRORS, _split_evidence, evidence_notes, extract_followups

# 본문에서 숨길 명세 블록(흐름도·화면 명세·HTML). 산출물은 따로 표시한다
SPEC_BLOCKS_RE = re.compile(r"```(?:flow|json|wireframe|html)\s*\n.*?```", re.S | re.I)
SEARCH_TERM_RE = re.compile(r"[\s,.?!·/()\"'`~:;]+")

CHECK_REASONS = {
    "low": "확신도 낮음",
    "down": "👎",
    "escalate": "확인 요청",
    "error": "오류",
    "limit": "한도 도달",
    "widened": "범위 미스",
}
ROUTE_KO = {"simple": "빠른 답", "agent": "심층 탐색", "wireframe": "와이어프레임", "clarify": "되묻기",
            "refuse": "거절", "unavailable": "API 사용 불가"}
PROVIDER_KO = {"api": "API 키", "subscription": "구독"}


def search_terms(query: str) -> list[str]:
    return [t for t in SEARCH_TERM_RE.split(query.strip()) if len(t) >= 2][:8]


def parse_answer(text: str | None) -> dict:
    """저장된 답변 원문 → 본문·근거·후속 질문. 슬랙과 같은 파서를 쓴다."""
    text = text or ""
    followups, rest = extract_followups(text)
    has_flow = "```flow" in rest
    has_spec = bool(re.search(r"```(?:json|wireframe|html)", rest))
    rest = SPEC_BLOCKS_RE.sub("", rest)
    body, evidence = _split_evidence(rest)
    return {"body": body.strip(), "evidence": evidence, "followups": followups,
            "has_flow": has_flow, "has_spec": has_spec}


@dataclass
class Freshness:
    state: str  # ok | changed | unknown
    changed: list[str] = field(default_factory=list)
    checked: int = 0
    reason: str = ""


@dataclass
class Row:
    id: str
    ts: float
    source: str
    user_id: str | None
    channel: str | None
    question: str
    commit: str | None
    status: str | None
    route: str | None
    provider: str | None  # api | subscription — 답변 시점에 실제 쓰인 인증 경로
    confidence: str | None
    cost: float
    duration_ms: int
    error: str | None
    answer: str | None
    scope: dict
    feedback: list[str]
    widened: bool
    resolved: dict | None = None  # 처리됨이면 {ts, operator, note}. 처리 뒤 새 👎·확인 요청이 오면 다시 열린다

    @property
    def domains(self) -> list[str]:
        return list(self.scope.get("domains") or [])

    @property
    def has_answer(self) -> bool:
        return self.status == "ok" and self.route in ("simple", "agent", "wireframe")

    @property
    def check_reasons(self) -> list[str]:
        r = []
        if self.confidence == "low" and self.route in ("simple", "agent", "wireframe"):
            r.append("low")
        if "down" in self.feedback:
            r.append("down")
        if "escalate" in self.feedback:
            r.append("escalate")
        if self.status == "error" or self.route == "unavailable":
            r.append("error")
        if self.error in LIMIT_ERRORS:
            r.append("limit")
        if self.widened:
            r.append("widened")
        return r


class ConsoleData:
    def __init__(self, db_path: Path, repo_name: str, web_base: str, results_dir: Path,
                 domain_names: dict[str, str] | None = None):
        if not db_path.exists():
            raise FileNotFoundError(f"감사 로그 DB가 없습니다: {db_path} — 먼저 askcode sync")
        # 읽기 전용 연결: 콘솔은 감사 로그를 바꾸지 않는다
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.repo = repo_name
        self.web_base = web_base
        self.results_dir = results_dir
        self.domain_names = domain_names or {}

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    # ---------- 질문 ----------
    def latest_commit(self) -> str | None:
        r = self.q("SELECT commit_sha FROM repo_state WHERE repo=?", (self.repo,))
        return r[0]["commit_sha"] if r else None

    def rows(self, source: str | None = "slack", since: float | None = None) -> list[Row]:
        where, params = [], []
        if source and source != "all":
            where.append("q.source=?")
            params.append(source)
        if since:
            where.append("q.ts>=?")
            params.append(since)
        sql = ("SELECT q.*, r.status, r.route, r.provider, r.confidence, r.cost_usd, r.duration_ms, r.error, r.answer, r.scope "
               "FROM queries q LEFT JOIN query_results r ON r.query_id=q.id"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY q.ts DESC")
        fb: dict[str, list[str]] = {}
        for f in self.q("SELECT query_id, value FROM feedback"):
            fb.setdefault(f["query_id"], []).append(f["value"])
        widened = {r["query_id"] for r in self.q("SELECT DISTINCT query_id FROM tool_calls WHERE tool='widen_scope'")}
        resolved = self._resolved()
        out = []
        for r in self.q(sql, tuple(params)):
            try:
                scope = json.loads(r["scope"]) if r["scope"] else {}
            except ValueError:
                scope = {}
            out.append(Row(r["id"], r["ts"], r["source"], r["user_id"], r["channel"], r["question"], r["commit_sha"],
                           r["status"], r["route"], r["provider"], r["confidence"], r["cost_usd"] or 0.0,
                           r["duration_ms"] or 0, r["error"], r["answer"], scope, fb.get(r["id"], []),
                           r["id"] in widened, resolved.get(r["id"])))
        return out

    def _resolved(self) -> dict[str, dict]:
        """질문별 처리 상태. 마지막 기록이 resolve이고, 그 뒤에 새 👎·확인 요청이 없을 때만 처리됨."""
        try:
            recs = self.q("SELECT query_id, ts, operator, action, note FROM resolutions ORDER BY ts, id")
        except sqlite3.OperationalError:  # 마이그레이션 전 DB(v3 이하)
            return {}
        last: dict[str, dict] = {}
        for r in recs:
            last[r["query_id"]] = dict(r)
        late_fb = {}
        for f in self.q("SELECT query_id, MAX(ts) AS ts FROM feedback WHERE value IN ('down', 'escalate') GROUP BY query_id"):
            late_fb[f["query_id"]] = f["ts"]
        return {qid: {"ts": r["ts"], "operator": r["operator"], "note": r["note"]}
                for qid, r in last.items()
                if r["action"] == "resolve" and late_fb.get(qid, 0) <= r["ts"]}

    def search(self, query: str = "", *, source: str | None = "slack", domain: str = "", route: str = "",
               confidence: str = "", feedback: str = "", days: int = 0) -> list[Row]:
        since = time.time() - days * 86400 if days else None
        rows = self.rows(source, since)
        if domain:
            rows = [r for r in rows if domain in r.domains]
        if route:
            rows = [r for r in rows if r.route == route]
        if confidence:
            rows = [r for r in rows if r.confidence == confidence]
        if feedback:
            rows = [r for r in rows if feedback in r.feedback]
        terms = search_terms(query)
        if not terms:
            return rows
        scored = []
        for r in rows:
            qtext, atext = r.question.lower(), parse_answer(r.answer)["body"].lower()
            hits = [t.lower() for t in terms if t.lower() in qtext or t.lower() in atext]
            if not hits:
                continue
            # 질문에 걸린 단어를 더 무겁게, 걸린 단어 수가 많을수록 위로
            score = len(hits) * 10 + sum(3 for t in hits if t in qtext) + sum(atext.count(t) for t in hits) * 0.1
            scored.append((score, r))
        scored.sort(key=lambda x: (x[0], x[1].ts), reverse=True)
        return [r for _, r in scored]

    def get(self, query_id: str) -> Row | None:
        for r in self.rows(source="all"):
            if r.id == query_id:
                return r
        return None

    def citations(self, row: Row) -> list[dict]:
        notes = evidence_notes(row.answer or "")
        sha = row.commit or "main"
        out = []
        for c in self.q("SELECT source, path, start_line, end_line, verified, reason FROM citations "
                        "WHERE query_id=? ORDER BY id", (row.id,)):
            doc = self.q("SELECT url, source FROM doc_files WHERE path=?", (c["path"],))
            if doc and doc[0]["source"] != "repo" and doc[0]["url"]:
                url = doc[0]["url"]
            elif not c["start_line"]:
                url = f"{self.web_base}/tree/{sha}/{c['path']}"
            else:
                url = f"{self.web_base}/blob/{sha}/{c['path']}#L{c['start_line']}" + (
                    f"-L{c['end_line']}" if c["end_line"] != c["start_line"] else "")
            out.append({"source": c["source"], "path": c["path"], "start": c["start_line"] or 0,
                        "end": c["end_line"] or 0, "verified": bool(c["verified"]), "reason": c["reason"],
                        "url": url, "note": notes.get((c["path"], c["start_line"] or 0), "")})
        return out

    def tool_steps(self, query_id: str) -> list[tuple[str, dict]]:
        steps = []
        for t in self.q("SELECT tool, input_json FROM tool_calls WHERE query_id=? ORDER BY id", (query_id,)):
            try:
                steps.append((t["tool"], json.loads(t["input_json"])))
            except ValueError:
                steps.append((t["tool"], {}))
        return steps

    def usage(self, query_id: str) -> dict:
        r = self.q("SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, num_turns, provider "
                   "FROM query_results WHERE query_id=?", (query_id,))
        return dict(r[0]) if r else {}

    def feedback_events(self, query_id: str) -> list[dict]:
        return [dict(f) for f in self.q("SELECT ts, user_id, value FROM feedback WHERE query_id=? ORDER BY ts",
                                        (query_id,))]

    # ---------- 최신성 ----------
    def freshness(self, row: Row, cites: list[dict]) -> Freshness:
        """검증된 코드 근거 파일이 답변 기준 커밋 이후 바뀌었는지, 커밋별 파일 해시로 비교한다."""
        paths = sorted({c["path"] for c in cites if c["verified"] and c["source"] == "code"})
        latest = self.latest_commit()
        if not paths:
            return Freshness("unknown", reason="검증된 코드 근거가 없어요")
        if not row.commit or not latest:
            return Freshness("unknown", reason="기준 커밋을 알 수 없어요")
        if row.commit == latest:
            return Freshness("ok", checked=len(paths))

        def snapshot(sha: str) -> dict[str, str] | None:
            rows = self.q("SELECT path, blob FROM commit_files WHERE repo=? AND commit_sha=?", (self.repo, sha))
            return {r["path"]: r["blob"] for r in rows} if rows else None

        old, new = snapshot(row.commit), snapshot(latest)
        if old is None or new is None:
            return Freshness("unknown", reason="커밋 스냅숏이 없어 비교할 수 없어요")
        changed = []
        for p in paths:
            if any(k == p or k.startswith(p.rstrip("/") + "/") for k in old):  # 파일 또는 폴더
                keys = [k for k in old.keys() | new.keys() if k == p or k.startswith(p.rstrip("/") + "/")]
                if any(old.get(k) != new.get(k) for k in keys):
                    changed.append(p)
        return Freshness("changed" if changed else "ok", changed, len(paths))

    # ---------- 통계·확인 필요 ----------
    def stats(self, days: int = 7) -> dict:
        since = time.time() - days * 86400
        all_rows = self.rows(source="all", since=since)
        slack = [r for r in all_rows if r.source == "slack"]
        return {
            "questions": len(slack),
            "cost_all": sum(r.cost for r in all_rows),
            "cost_slack": sum(r.cost for r in slack),
            "by_route": {k: sum(1 for r in slack if r.route == k) for k in ROUTE_KO},
            "check": sum(1 for r in slack if r.check_reasons and not r.resolved),
        }

    def needs_check(self, source: str = "slack", days: int = 30, resolved: bool = False) -> list[Row]:
        """확인 필요 목록. 기본은 아직 처리 안 된 건만, resolved=True면 처리된 건만."""
        return [r for r in self.rows(source, time.time() - days * 86400)
                if r.check_reasons and bool(r.resolved) == resolved]

    # ---------- 평가 ----------
    def eval_runs(self) -> list[dict]:
        runs = []
        for f in sorted(self.results_dir.glob("eval-*.json"), reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            runs.append({"name": f.stem, "summary": d.get("summary", {}), "results": d.get("results", [])})
        return runs

    def eval_run(self, name: str) -> dict | None:
        return next((r for r in self.eval_runs() if r["name"] == name), None)


def compare_runs(a: dict, b: dict) -> list[dict]:
    """두 평가 회차의 문항별 비교. a가 이전, b가 이후."""
    ra = {r["id"]: r for r in a["results"]}
    out = []
    for r in b["results"]:
        prev = ra.get(r["id"])
        before, after = (prev or {}).get("correct"), r.get("correct")
        if prev is None:
            change = "새 문항"
        elif before is not True and after is True:
            change = "새로 맞음"
        elif before is True and after is not True:
            change = "새로 틀림"
        else:
            change = "그대로"
        out.append({"id": r["id"], "question": r.get("question", ""), "before": before, "after": after,
                    "change": change, "reason": r.get("judge_reason", ""), "cost": r.get("cost_usd", 0.0)})
    order = {"새로 틀림": 0, "새로 맞음": 1, "새 문항": 2, "그대로": 3}
    return sorted(out, key=lambda x: (order[x["change"]], x["id"]))
