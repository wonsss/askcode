"""문서 검색·읽기 도구 (레포 docs, 노션, 슬랙 지정 채널)."""

from __future__ import annotations

import re

from askcode.store.db import Store
from askcode.tools.code_tools import ToolSession

SOURCE_KO = {"repo": "레포 문서", "notion": "노션", "slack": "슬랙"}


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"[\s,.?!·/()\"'`]+", query) if len(t) >= 2][:8]


def _diversify(scored: list, limit: int, reserved: int) -> list:
    """상위 limit건을 고르되, 노션·슬랙 결과가 있으면 최대 reserved자리를 보장한다.

    레포 문서가 같은 주제를 많이 다루면 노션 기획서가 순위에서 밀려, 기획서와 코드를 비교하는 질문에서
    에이전트가 기획서를 보지 못했다(q26).
    """
    top = scored[:limit]
    extra = [x for x in scored[limit:] if x[2]["source"] != "repo"][:reserved]
    have = sum(1 for x in top if x[2]["source"] != "repo")
    for x in extra[: max(0, reserved - have)]:
        repo_idx = [i for i, y in enumerate(top) if y[2]["source"] == "repo"]
        if not repo_idx:
            break
        top[repo_idx[-1]] = x
    return sorted(top, key=lambda x: (x[0], x[1]), reverse=True)


class DocTools:
    def __init__(self, store: Store, max_results: int = 8, max_lines: int = 120):
        self.store = store
        self.max_results = max_results
        self.max_lines = max_lines

    def search(self, session: ToolSession, query: str, source: str | None = None) -> str:
        terms = _terms(query)
        if not terms:
            return "검색어가 너무 짧습니다."
        # 3글자 이상은 FTS5(trigram)로 후보를 넓게, 2글자 한국어 단어는 LIKE로 보완해 점수를 매긴다
        long_terms = [t for t in terms if len(t) >= 3]
        candidates: set[int] = set()
        if long_terms:
            fts = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            candidates |= {r[0] for r in self.store.execute(
                "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? LIMIT 400", (fts,))}
        for t in terms:
            if len(t) < 3:
                candidates |= {r[0] for r in self.store.execute(
                    "SELECT id FROM docs WHERE text LIKE ? OR title LIKE ? LIMIT 400", (f"%{t}%", f"%{t}%"))}
        if not candidates:
            return f"'{query}' 문서 결과 없음."
        placeholders = ",".join("?" * len(candidates))
        rows = self.store.execute(
            f"SELECT id, source, path, title, heading, url, updated_at, start_line, end_line, text FROM docs "
            f"WHERE id IN ({placeholders})" + (" AND source=?" if source else ""),
            (*candidates, *([source] if source else [])),
        ).fetchall()
        scored = []
        for r in rows:
            if r["source"] == "repo" and not session.in_scope(r["path"]):
                continue
            hay = f"{r['title']} {r['heading'] or ''} {r['text']}".lower()
            score = sum(hay.count(t.lower()) for t in terms) + 3 * sum(t.lower() in (r["title"] + (r["heading"] or "")).lower() for t in terms)
            covered = sum(t.lower() in hay for t in terms)
            scored.append((covered, score, r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out = []
        for covered, score, r in _diversify(scored, self.max_results, reserved=0 if source else 3):
            snippet = _snippet(r["text"], terms)
            when = f" · 수정 {r['updated_at'][:10]}" if r["updated_at"] else ""
            out.append(f"[{SOURCE_KO.get(r['source'], r['source'])}] {r['path']}:{r['start_line']}-{r['end_line']} "
                       f"「{r['title']}{' > ' + r['heading'] if r['heading'] else ''}」{when}\n    {snippet}")
            session.mark(r["path"], r["start_line"], r["end_line"])
        if not out:
            return f"'{query}' 문서 결과 없음(질문 범위 안)."
        return f"문서 {len(scored)}건 중 상위 {len(out)}건\n" + "\n".join(out)

    def read(self, session: ToolSession, path: str, start: int = 1, end: int | None = None) -> str:
        f = self.store.doc_file(path)
        if not f:
            return f"{path}: 문서 인덱스에 없습니다. search_docs로 경로를 먼저 찾으세요."
        if f["source"] == "repo" and not session.in_scope(path):
            return f"{path}: 질문 범위 밖 문서입니다. 꼭 필요하면 widen_scope."
        lines = f["body"].splitlines()
        total = len(lines)
        start = max(1, start)
        end = min(total, end if end else start + self.max_lines - 1, start + self.max_lines - 1)
        body = "\n".join(f"{i:>5}  {lines[i - 1][:400]}" for i in range(start, end + 1))
        session.mark(path, start, end)
        meta = f"「{f['title']}」 {SOURCE_KO.get(f['source'], f['source'])}" + (f" · 수정 {f['updated_at'][:10]}" if f["updated_at"] else "")
        tail = f"\n… (전체 {total}줄, 다음 구간은 start={end + 1})" if end < total else ""
        return f"{path} [{start}-{end} / {total}줄] {meta}\n{body}{tail}"


def _snippet(text: str, terms: list[str], width: int = 160) -> str:
    low = text.lower()
    pos = min((low.find(t.lower()) for t in terms if t.lower() in low), default=0)
    s = max(0, pos - 40)
    return re.sub(r"\s+", " ", text[s: s + width]).strip()
