"""질의 처리 파이프라인 (1일차: 코드 Q&A).

질문 기록 → 에이전트 실행(읽기 전용 도구) → 인용 검증 → 확신도 판정 → 결과 기록.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from askcode.agent.base import AgentRequest, AgentRunner, ToolSpec
from askcode.config import RepoConfig
from askcode.pipeline.prompts import build_system_prompt
from askcode.store.db import QueryResult, Store
from askcode.tools.code_tools import CodeTools, ToolSession

CITATION_RE = re.compile(r"(?<![\w/.-])([\w@.\-]+(?:/[\w@.\-\[\]()]+)+\.\w+):(\d+)(?:\s*-\s*(\d+))?")
EVIDENCE_HEADER_RE = re.compile(r"^\s*(?:#+\s*)?\**\s*근거\s*[:：]?\s*\**\s*[:：]?\s*$", re.MULTILINE)
OVERVIEW_MAX_CHARS = 8000


@dataclass
class Citation:
    path: str
    start: int
    end: int
    note: str
    verified: bool
    reason: str
    url: str


@dataclass
class Answer:
    query_id: str
    body: str
    citations: list[Citation]
    confidence: str  # high | medium | low
    commit: str
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None
    unverified: list[Citation] = field(default_factory=list)


def _split_evidence(text: str) -> tuple[str, str]:
    m = None
    for m in EVIDENCE_HEADER_RE.finditer(text):
        pass  # 마지막 "근거:" 머리를 사용
    if not m:
        return text.strip(), ""
    return text[: m.start()].strip(), text[m.end() :].strip()


class AnswerService:
    def __init__(self, store: Store, repo: RepoConfig, repo_path: Path, runner: AgentRunner,
                 code_tools: CodeTools, max_turns: int = 12, max_budget_usd: float | None = None):
        self.store = store
        self.repo = repo
        self.repo_path = repo_path
        self.runner = runner
        self.code_tools = code_tools
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd

    def _overview(self) -> str:
        parts = []
        for name in self.repo.context_files:
            p = self.repo_path / name
            if p.is_file() and self.code_tools.filter.allowed(name):
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
        return "\n\n".join(parts)[:OVERVIEW_MAX_CHARS]

    def _tools(self, query_id: str, session: ToolSession) -> list[ToolSpec]:
        ct, store = self.code_tools, self.store

        async def code_search(args: dict) -> str:
            out = ct.search(session, args["query"], args.get("path_glob"), fixed=not args.get("regex", False))
            store.log_tool_call(query_id, "code_search", args, out.splitlines()[0][:200])
            return out

        async def read_code(args: dict) -> str:
            out = ct.read(session, args["path"], int(args.get("start", 1)), args.get("end"))
            store.log_tool_call(query_id, "read_code", args, out.splitlines()[0][:200])
            return out

        async def list_dir(args: dict) -> str:
            out = ct.list_dir(session, args.get("prefix", ""))
            store.log_tool_call(query_id, "list_dir", args, out.splitlines()[0][:200])
            return out

        return [
            ToolSpec(
                "code_search",
                "레포 전체에서 문자열(기본) 또는 정규식을 검색해 '파일:줄: 내용'을 최대 20건 돌려준다. "
                "한국어 화면 문구, 영문 식별자, API 경로 등으로 위치를 찾을 때 먼저 사용한다. "
                "path_glob(예: 'apps/api/**')으로 범위를 좁힐 수 있다.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "검색어"},
                        "path_glob": {"type": "string", "description": "검색 범위 글롭(선택)"},
                        "regex": {"type": "boolean", "description": "정규식으로 검색(기본 false)"},
                    },
                    "required": ["query"],
                },
                code_search,
            ),
            ToolSpec(
                "read_code",
                "파일의 지정 구간을 줄 번호와 함께 읽는다(한 번에 최대 200줄). code_search로 찾은 위치 "
                "주변만 읽고, 파일 전체를 읽지 않는다.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "레포 기준 상대 경로"},
                        "start": {"type": "integer", "description": "시작 줄(기본 1)"},
                        "end": {"type": "integer", "description": "끝 줄(선택)"},
                    },
                    "required": ["path"],
                },
                read_code,
            ),
            ToolSpec(
                "list_dir",
                "디렉터리 바로 아래 항목과 파일 수를 보여준다. 구조 파악용이며 깊은 나열은 하지 않는다.",
                {
                    "type": "object",
                    "properties": {"prefix": {"type": "string", "description": "예: 'apps/api/src'"}},
                    "required": [],
                },
                list_dir,
            ),
        ]

    def _verify(self, query_id: str, evidence: str, session: ToolSession, commit: str) -> tuple[list[Citation], list[Citation]]:
        ok: list[Citation] = []
        bad: list[Citation] = []
        seen: set[tuple[str, int, int]] = set()
        for line in evidence.splitlines() or [evidence]:
            for m in CITATION_RE.finditer(line):
                path, start = m.group(1), int(m.group(2))
                end = int(m.group(3) or start)
                if end < start:
                    start, end = end, start
                if (path, start, end) in seen:
                    continue
                seen.add((path, start, end))
                note = line[m.end():].strip(" -—:·")
                info = self.store.file_info(self.repo.name, path)
                if not info:
                    reason, verified = "인덱스에 없는 파일", False
                elif end > info["lines"]:
                    reason, verified = f"파일은 {info['lines']}줄", False
                elif not session.covers(path, start, end):
                    reason, verified = "도구로 확인하지 않은 구간", False
                else:
                    reason, verified = "ok", True
                url = f"{self.repo.web_base}/blob/{commit}/{path}#L{start}" + (f"-L{end}" if end != start else "")
                c = Citation(path, start, end, note, verified, reason, url)
                (ok if verified else bad).append(c)
                self.store.log_citation(query_id, "code", path, start, end, verified, reason)
        return ok, bad

    async def answer(self, question: str, source: str = "cli", user_id: str | None = None,
                     channel: str | None = None, thread_ts: str | None = None) -> Answer:
        commit = self.store.get_repo_commit(self.repo.name) or "unknown"
        query_id = self.store.log_query(question, source, user_id, channel, thread_ts, commit)
        session = ToolSession()
        system = build_system_prompt(self.repo.name, self.repo.description, self.repo.ref, commit, self._overview())
        resp = await self.runner.run(
            AgentRequest(question, system, self._tools(query_id, session), self.max_turns, self.max_budget_usd)
        )
        body, evidence = _split_evidence(resp.text)
        verified, unverified = self._verify(query_id, evidence or resp.text, session, commit)
        if resp.error and not body:
            confidence = "low"
            body = "답변을 만들지 못했습니다. 잠시 후 다시 시도하거나 질문 범위를 좁혀 주세요."
        elif verified and not unverified:
            confidence = "high"
        elif verified:
            confidence = "medium"
        else:
            confidence = "low"
        self.store.log_result(
            query_id,
            QueryResult(
                status="error" if resp.error and not resp.text else "ok",
                answer=resp.text, confidence=confidence, model=resp.model,
                input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                cache_read_tokens=resp.cache_read_tokens, cache_write_tokens=resp.cache_write_tokens,
                cost_usd=resp.cost_usd, duration_ms=resp.duration_ms, num_turns=resp.num_turns, error=resp.error,
            ),
        )
        return Answer(query_id, body, verified, confidence, commit, resp.cost_usd, resp.duration_ms, resp.error, unverified)
