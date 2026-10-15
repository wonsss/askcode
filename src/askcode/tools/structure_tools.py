"""구조 맵 기반 추적 도구. 에이전트가 grep을 반복하지 않고 한 번에 레이어를 건너뛰게 한다."""

from __future__ import annotations

import json
import re
from pathlib import Path

from askcode.store.db import Store
from askcode.tools.code_tools import ToolSession

PRESENTATION_KO = {
    "native": "네이티브 화면",
    "embedded": "웹뷰(웹 /embed 화면을 앱 안에서 표시)",
    "external": "외부 브라우저/외부 앱",
    "web-only": "웹 전용(앱에서 미지원)",
}

class StructureTools:
    def __init__(self, store: Store, repo: str, repo_path: Path):
        self.store = store
        self.repo = repo
        self.repo_path = repo_path

    def _rows(self, kind: str):
        return self.store.execute(
            "SELECT key, label, path, line, extra FROM structure WHERE repo=? AND kind=?", (self.repo, kind)
        ).fetchall()

    def find_api(self, session: ToolSession, query: str, limit: int = 12) -> str:
        """API 경로·api-client 함수명·컨트롤러명으로 검색해 클라이언트 → 서버 연결을 보여준다."""
        q = query.lower().strip()
        terms = [t for t in re.split(r"[\s,]+", q) if t]
        hits = []
        for r in self._rows("api"):
            x = json.loads(r["extra"])
            hay = " ".join(str(v) for v in (r["key"], x.get("client_fn"), x.get("controller"), x.get("raw_path"))).lower()
            if all(t in hay for t in terms):
                hits.append((r, x))
        if not hits:
            return f"'{query}'와 맞는 API가 없습니다. 경로 일부(예: 'follows', 'search')나 함수명으로 다시 찾아보세요."
        lines = []
        for r, x in hits[:limit]:
            parts = [f"{r['key']}"]
            if x.get("client_fn"):
                parts.append(f"클라이언트 `{x['client_fn']}` ({r['path']}:{r['line']})")
                session.mark(r["path"], r["line"], r["line"] + 3)
            else:
                parts.append("클라이언트 호출 없음(관리자·내부·외부 연동용 가능)")
            if x.get("controller"):
                parts.append(f"서버 `{x['controller']}` ({x['controller_path']}:{x['controller_line']})")
                session.mark(x["controller_path"], x["controller_line"], x["controller_line"] + 3)
            else:
                parts.append("서버 핸들러를 찾지 못함")
            lines.append(" → ".join(parts))
        more = f"\n… 외 {len(hits) - limit}건" if len(hits) > limit else ""
        return f"API {len(hits)}건\n" + "\n".join(lines) + more

    def _client_calls_in(self, rel: str) -> list[str]:
        p = self.repo_path / rel
        if not p.is_file():
            return []
        text = p.read_text(encoding="utf-8", errors="replace")
        fns = {json.loads(r["extra"]).get("client_fn") for r in self._rows("api")} - {None}
        return sorted(fn for fn in fns if re.search(rf"\.{re.escape(fn)}\(", text))

    def find_screen(self, session: ToolSession, query: str, limit: int = 10) -> str:
        """화면(URL·라우트·manifest id·키워드)을 찾아 플랫폼별 구현 방식과 화면 파일, 직접 호출하는 API를 보여준다."""
        q = query.strip().lower()
        out: list[str] = []
        manifest = [r for r in self._rows("manifest")
                    if q in r["key"].lower() or any(q in p.lower() or _pattern_matches(p, q)
                                                    for p in json.loads(r["extra"])["patterns"])]
        for r in manifest[:limit]:
            x = json.loads(r["extra"])
            pres = x.get("presentation")
            target = x.get("target")
            change = f" (목표: {PRESENTATION_KO.get(target, target)})" if target and target != pres else ""
            out.append(f"[앱 화면 manifest] `{r['key']}` {', '.join(x['patterns'])} → {PRESENTATION_KO.get(pres, pres)}{change} "
                       f"({r['path']}:{r['line']})")
            session.mark(r["path"], r["line"], r["line"] + 8)
        routes = [r for r in self._rows("route") if q in r["key"].lower() or q in r["path"].lower()
                  or _pattern_matches(r["key"].split(" ", 1)[1], q)]
        for r in routes[:limit]:
            calls = self._client_calls_in(r["path"])
            api = f" · 직접 호출 API: {', '.join(calls[:6])}" if calls else ""
            out.append(f"[{r['label']} 라우트] {r['key'].split(' ', 1)[1]} — {r['path']}{api}")
            session.mark(r["path"], 1, 1)
        if not out:
            return (f"'{query}' 화면을 찾지 못했습니다. URL 경로(예: /bills/:slug)나 영문 키워드(bills, people)로 "
                    "다시 찾거나 code_search를 쓰세요.")
        return "\n".join(out)

    def find_bridge(self, session: ToolSession, query: str = "") -> str:
        """WebView ↔ 앱 메시지 타입별로 웹(송신)·앱(수신) 위치를 보여준다."""
        q = query.strip().lower()
        groups: dict[str, list] = {}
        for r in self._rows("bridge"):
            if q and q not in r["key"].lower():
                continue
            groups.setdefault(r["key"], []).append(r)
        if not groups:
            return f"'{query}' 브리지 메시지가 없습니다."
        out = []
        for msg, rows in sorted(groups.items()):
            out.append(f"■ {msg}")
            for r in sorted(rows, key=lambda r: (r["label"], r["path"], r["line"]))[:8]:
                code = json.loads(r["extra"]).get("code", "")
                out.append(f"  - [{r['label']}] {r['path']}:{r['line']}  {code[:110]}")
                session.mark(r["path"], r["line"], r["line"])
        return "\n".join(out)

def _pattern_matches(pattern: str, url: str) -> bool:
    """'/bills/:slug/:section?' 같은 패턴이 '/bills/abc' 같은 URL과 맞는지."""
    if not url.startswith("/"):
        return False
    pseg = [s for s in pattern.strip("/").split("/") if s]
    useg = [s for s in url.strip("/").split("/") if s]
    required = [s for s in pseg if not s.endswith("?")]
    if not (len(required) <= len(useg) <= len(pseg)):
        return False
    return all(p.startswith(":") or p == u for p, u in zip(pseg, useg))
