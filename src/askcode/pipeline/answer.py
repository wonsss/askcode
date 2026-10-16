"""질의 처리 파이프라인.

1. 질문 기록
2. 스코핑 (소형 모델 + 도메인 맵) → 라우팅: 거절 · 되묻기 · 단순(소형 모델) · 에이전트(대형 모델)
3. 에이전트 실행 — 읽기 전용 도구, 스코프 하드 필터(1회 확장 가능)
4. 인용 검증 — 인덱스(코드·문서) 존재, 라인 범위, 도구로 실제 본 구간
5. 확신도 판정 → 결과 기록
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from askcode.agent.base import AgentRequest, AgentRunner, ToolSpec
from askcode.config import RepoConfig
from askcode.llm.pricing import estimate_cost
from askcode.pipeline.external import (
    EXTERNAL_ONLY_NO_PROMPT, EXTERNAL_ONLY_TEXT, build_external_prompt, internal_terms,
)
from askcode.pipeline.flowchart import FlowSpecError, extract_flow, flow_filename, parse_flow, render_flow
from askcode.pipeline.prompts import (
    build_change_mockup_question, build_question, build_system_prompt, build_wireframe_question,
)
from askcode.pipeline.scoping import DomainMap, LLMUnavailable, Route, Scope, Scoper, apply_screenshot_rule, route_for
from askcode.pipeline.screenshot import ScreenCapture, screenshot_block, screenshot_context
from askcode.pipeline.wireframe import (
    WireframeSpecError, extract_html, extract_spec, has_proposal, parse_spec, render_spec, sanitize_html,
    wireframe_filename,
)
from askcode.store.db import QueryResult, Store
from askcode.tools.code_tools import CodeTools, ToolSession
from askcode.tools.doc_tools import DocTools
from askcode.tools.structure_tools import StructureTools

CITATION_RE = re.compile(r"(?<![\w/.-])([\w@.\-가-힣]+(?:/[\w@.\-\[\]()가-힣]+)+\.\w+):(\d+)(?:\s*-\s*(\d+))?")
EVIDENCE_HEADER_RE = re.compile(r"^\s*(?:#+\s*)?\**\s*근거\s*[:：]?\s*\**\s*[:：]?\s*$", re.MULTILINE)
OVERVIEW_MAX_CHARS = 8000

REFUSE_TEXT = (
    "이 질문은 AskCode가 답할 수 없는 범위예요. AskCode는 코드와 기획 문서만 읽으며, "
    "실제 사용자 정보·운영 데이터·비밀값에는 접근하지 않습니다. 필요한 경우 담당 부서의 정식 절차로 요청해 주세요."
)
UNSUPPORTED_TEXT = (
    "이 요청은 AskCode가 하지 않는 작업이에요. AskCode는 코드·기획 문서를 읽고 답하며, 새 화면의 와이어프레임을 "
    "만들 수 있어요. 코드 수정·배포·문서 작성은 하지 않습니다."
)
WIREFRAME_MISSING = "와이어프레임을 그리지 못했어요. 화면을 하나로 좁혀 다시 요청해 주세요. (예: \"커뮤니티 글 목록 화면 와이어프레임\")"
COMPARISON_MISSING = "변경안을 표시해 그리지 못해 받은 그림만 올렸어요. 화면 하나와 바꿀 점 하나로 좁혀 다시 요청해 주세요."
DEV_NOTE_PATH_RE = re.compile(r"`([\w@.\-\[\]()가-힣]+(?:/[\w@.\-\[\]()가-힣]+)+\.\w+)`")
LIMIT_TEXT = ("질문 하나에 쓸 수 있는 탐색 한도(비용·횟수)에 도달해 답을 끝맺지 못했어요. "
              "질문을 더 좁혀서 다시 물어봐 주세요. (예: 화면 하나, 조건 하나)")
LIMIT_ERRORS = ("error_max_budget_usd", "error_max_turns")
EXTERNAL_AGENT_NOTE = (
    "- 외부 조사 항목(시스템이 답 아래에 사외 AI용 조사 프롬프트로 따로 붙입니다): {topics}\n"
    "  이 항목은 답하지 않습니다. 일반론·업계 관행·흔한 방식·추정 단락을 쓰지 않고, \"외부 자료를 조회하지 못했다\"는 말이나"
    " 확인 필요 항목에도 넣지 않습니다. 코드·문서로 확인한 내용만 씁니다.\n"
)
UNAVAILABLE_TEXT = "지금은 AI 모델을 호출할 수 없어 답하지 못했어요(크레딧·인증 문제). 관리자에게 알려 주세요."


def _wireframe_attachment(text: str, filename: str) -> tuple[tuple[str, str] | None, str, dict | None]:
    """명세(```json)가 있으면 렌더러로 그리고, 없으면 자유 HTML을 정제한다. 명세가 깨지면 첨부 없음.

    (첨부, 명세 블록을 뺀 텍스트, 읽은 명세 또는 None)
    """
    raw_spec, rest = extract_spec(text)
    if raw_spec is not None:
        try:
            spec = parse_spec(raw_spec)
            return (filename, render_spec(spec)), rest, spec
        except WireframeSpecError:
            return None, rest, None
    raw_html, rest = extract_html(text)
    return ((filename, sanitize_html(raw_html)) if raw_html else None), rest, None


def _mark_unchecked_dev_paths(body: str, verified_paths: set[str]) -> str:
    """'🔧 개발자 참고' 줄의 파일 경로 중 검증된 근거에 없는 것에 표시를 붙인다. 근거 없는 위치를 단정하지 않게."""
    def mark(m: re.Match) -> str:
        return m.group(0) if m.group(1) in verified_paths else f"{m.group(0)}(근거 미확인)"
    return "\n".join(DEV_NOTE_PATH_RE.sub(mark, line) if line.lstrip().startswith("🔧") else line
                     for line in body.splitlines())


@dataclass
class Citation:
    source: str  # code | doc | notion | slack
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
    status: str = "answer"  # answer | clarify | refuse | error
    route: str = "agent"
    scope: Scope | None = None
    options: list[tuple[str, str]] = field(default_factory=list)  # (domain id, 이름)
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None
    unverified: list[Citation] = field(default_factory=list)
    config_dependent: bool = False
    tool_calls: int = 0
    domain_names: list[str] = field(default_factory=list)  # 슬랙 표시용 도메인 이름
    followups: list[str] = field(default_factory=list)  # 슬랙 '이어서 물어보기' 버튼
    attachment: tuple[str, str] | None = None  # (파일 이름, 정제된 HTML) — 와이어프레임·흐름도
    attachment_kind: str | None = None  # wireframe | flow
    comparison: bool = False  # 스크린샷 기반 변경안 목업 (원본 스크린샷과 비교)
    vision_cost_usd: float = 0.0  # 스크린샷 문구 추출 비용 (cost_usd에 포함)
    provider: str | None = None  # api | subscription — 슬랙 메타 줄에 표시
    external_prompt: str | None = None  # 사외 AI에 붙여넣을 외부 조사 프롬프트 (내부 식별자 필터 통과분)
    external_dropped: int = 0  # 내부 정보가 섞일 수 있어 뺀 조사 항목 수 (전부 빠지면 prompt 없이 안내만)


def _split_evidence(text: str) -> tuple[str, str]:
    m = None
    for m in EVIDENCE_HEADER_RE.finditer(text):
        pass  # 마지막 "근거:" 머리를 사용
    if not m:
        return text.strip(), ""
    return text[: m.start()].strip(), text[m.end():].strip()


FOLLOWUP_HEADER_RE = re.compile(r"^\s*(?:#+\s*)?\**\s*후속\s*질문\s*\**\s*[:：]?\s*\**\s*$", re.MULTILINE)
FOLLOWUP_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
FOLLOWUP_MAX_CHARS = 60  # 슬랙 버튼 글자 수 한도(75)보다 여유 있게
MAX_FOLLOWUPS = 3


def extract_followups(text: str) -> tuple[list[str], str]:
    """에이전트 출력 끝의 "후속 질문:" 목록을 떼어 낸다. (질문 목록, 목록을 뺀 텍스트)

    목록은 헤더 다음의 연속된 글머리표 줄까지다. 너무 길거나 자리표시자 그대로인 항목은 버린다.
    """
    m = None
    for m in FOLLOWUP_HEADER_RE.finditer(text or ""):
        pass  # 마지막 헤더를 쓴다
    if not m:
        return [], text
    lines = text[m.end():].splitlines()
    items, used = [], 0
    for line in lines:
        if not line.strip():
            used += 1
            if items:
                break
            continue
        im = FOLLOWUP_ITEM_RE.match(line)
        if not im:
            break
        used += 1
        q = im.group(1).replace("`", "").strip().strip("\"'“”").strip()
        if 4 <= len(q) <= FOLLOWUP_MAX_CHARS and not q.startswith("<") and q not in items:
            items.append(q)
    before, after = text[: m.start()].rstrip(), "\n".join(lines[used:]).strip()
    return items[:MAX_FOLLOWUPS], (before + "\n\n" + after if after else before).strip()


# 줄 번호 없는 폴더 근거: "- apps/mobile/app/(tabs)/ — 하단 탭 화면". 마지막 조각에 확장자가 있으면 파일이라 제외
DIR_CITATION_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*`?((?:[\w@\-\[\]()가-힣]+/)+[\w@\-\[\]()가-힣]+)/?`?(?=\s*(?:[—:·-]|$))(.*)$")


def evidence_notes(text: str) -> dict[tuple[str, int], str]:
    """저장된 답변의 근거 목록에서 (경로, 시작 줄) → 설명을 뽑는다. '근거 전체 보기'에서 쓴다."""
    _, evidence = _split_evidence(text or "")
    notes: dict[tuple[str, int], str] = {}
    for line in evidence.splitlines():
        for m in CITATION_RE.finditer(line):
            notes.setdefault((m.group(1), int(m.group(2))), line[m.end():].strip(" -—:·"))
        dm = None if CITATION_RE.search(line) else DIR_CITATION_RE.match(line)
        if dm:
            notes.setdefault((dm.group(1), 0), dm.group(2).strip(" -—:·"))
    return notes


class AnswerService:
    def __init__(self, store: Store, repo: RepoConfig, repo_path: Path, runner: AgentRunner, code_tools: CodeTools,
                 max_turns: int = 12, max_budget_usd: float | None = None, *, scoper: Scoper | None = None,
                 domain_map: DomainMap | None = None, doc_tools: DocTools | None = None,
                 structure_tools: StructureTools | None = None, models: dict[str, str] | None = None,
                 efforts: dict[str, str | None] | None = None, provider: str = "api"):
        self.store = store
        self.repo = repo
        self.repo_path = repo_path
        self.runner = runner
        self.code_tools = code_tools
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.scoper = scoper
        self.domain_map = domain_map
        self.doc_tools = doc_tools
        self.structure_tools = structure_tools
        self.models = models or {}
        self.efforts = efforts or {}  # tier → effort, None이면 모델 기본값
        self.provider = provider  # 답변에 실제 쓰인 인증 경로를 결과·표시에 남긴다

    # ---------- 도구 ----------
    def _overview(self) -> str:
        parts = []
        for name in self.repo.context_files:
            p = self.repo_path / name
            if p.is_file() and self.code_tools.filter.allowed(name):
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
        return "\n\n".join(parts)[:OVERVIEW_MAX_CHARS]

    def _tools(self, query_id: str, session: ToolSession) -> list[ToolSpec]:
        ct, store = self.code_tools, self.store

        def logged(name: str, fn):
            async def handler(args: dict) -> str:
                out = fn(args)
                store.log_tool_call(query_id, name, args, out.splitlines()[0][:200] if out else "")
                return out
            return handler

        def widen(args: dict) -> str:
            if session.scope_globs is None:
                return "이미 레포 전체가 범위입니다."
            if session.widened:
                return "범위 확장은 한 번만 할 수 있습니다. 지금까지 찾은 근거로 답하세요."
            session.widened = True
            return "범위를 레포 전체로 넓혔습니다(범위 미스로 기록됨)."

        obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
        s = {"type": "string"}
        tools = [
            ToolSpec("code_search",
                     "레포에서 문자열(기본) 또는 정규식을 검색해 '파일:줄: 내용'을 최대 20건 돌려준다. 한국어 화면 문구, "
                     "영문 식별자, API 경로로 위치를 찾을 때 쓴다. path_glob(예: 'apps/api/**')으로 좁힐 수 있다.",
                     obj({"query": s, "path_glob": s, "regex": {"type": "boolean"}}, ["query"]),
                     logged("code_search", lambda a: ct.search(session, a["query"], a.get("path_glob"),
                                                               fixed=not a.get("regex", False)))),
            ToolSpec("read_code", "파일의 지정 구간을 줄 번호와 함께 읽는다(최대 200줄). 찾은 위치 주변만 읽는다.",
                     obj({"path": s, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]),
                     logged("read_code", lambda a: ct.read(session, a["path"], int(a.get("start", 1)), a.get("end")))),
            ToolSpec("list_dir", "디렉터리 바로 아래 항목과 파일 수를 보여준다(구조 파악용).",
                     obj({"prefix": s}), logged("list_dir", lambda a: ct.list_dir(session, a.get("prefix", "")))),
            ToolSpec("widen_scope", "질문 범위(도메인) 밖까지 검색해야 할 때 한 번만 호출한다.",
                     obj({"reason": s}, ["reason"]), logged("widen_scope", widen)),
        ]
        if self.structure_tools:
            st = self.structure_tools
            tools += [
                ToolSpec("find_screen",
                         "화면을 URL(예: '/bills/abc'), 라우트, 키워드(예: 'bills')로 찾아 앱에서 네이티브/웹뷰/외부 중 "
                         "무엇으로 열리는지, 웹·앱 화면 파일, 화면이 직접 호출하는 API 함수를 보여준다.",
                         obj({"query": s}, ["query"]), logged("find_screen", lambda a: st.find_screen(session, a["query"]))),
                ToolSpec("find_api",
                         "API 경로 일부·api-client 함수명·컨트롤러명으로 찾아 '클라이언트 함수 → 서버 핸들러' 연결과 위치를 "
                         "보여준다. 여러 단어는 모두 포함하는 항목만.",
                         obj({"query": s}, ["query"]), logged("find_api", lambda a: st.find_api(session, a["query"]))),
                ToolSpec("find_bridge", "웹뷰 ↔ 앱 메시지 타입별 웹(송신)·앱(수신) 위치를 보여준다. query는 메시지 이름 일부(선택).",
                         obj({"query": s}), logged("find_bridge", lambda a: st.find_bridge(session, a.get("query", "")))),
            ]
        if self.doc_tools:
            dt = self.doc_tools
            tools += [
                ToolSpec("search_docs",
                         "레포 문서(ADR·계획), 노션 기획 문서, 지정 슬랙 채널 논의를 검색한다. 정책 의도·결정 배경·기획서 "
                         "확인에 쓴다. source로 repo|notion|slack을 지정할 수 있다.",
                         obj({"query": s, "source": {"type": "string", "enum": ["repo", "notion", "slack"]}}, ["query"]),
                         logged("search_docs", lambda a: dt.search(session, a["query"], a.get("source")))),
                ToolSpec("read_doc", "문서의 지정 구간을 줄 번호와 함께 읽는다(최대 120줄). 경로는 search_docs 결과를 쓴다.",
                         obj({"path": s, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]),
                         logged("read_doc", lambda a: dt.read(session, a["path"], int(a.get("start", 1)), a.get("end")))),
            ]
        return tools

    # ---------- 인용 검증 ----------
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
                code = self.store.file_info(self.repo.name, path)
                doc = self.store.doc_file(path)
                if doc and doc["source"] != "repo":
                    source, total, url = doc["source"], doc["lines"], doc["url"] or ""
                elif code or doc:
                    source = "doc" if path.endswith(".md") else "code"
                    total = code["lines"] if code else doc["lines"]
                    url = f"{self.repo.web_base}/blob/{commit}/{path}#L{start}" + (f"-L{end}" if end != start else "")
                else:
                    source, total, url = "code", 0, ""
                if not (code or doc):
                    reason, verified = "인덱스에 없는 파일", False
                elif end > total:
                    reason, verified = f"파일은 {total}줄", False
                elif not session.covers(path, start, end):
                    reason, verified = "도구로 확인하지 않은 구간", False
                else:
                    reason, verified = "ok", True
                c = Citation(source, path, start, end, note, verified, reason, url)
                (ok if verified else bad).append(c)
                self.store.log_citation(query_id, source, path, start, end, verified, reason)
            dm = None if CITATION_RE.search(line) else DIR_CITATION_RE.match(line)
            if dm and (dm.group(1), 0, 0) not in seen:
                # 폴더 근거는 줄 번호 0으로 기록한다. list_dir로 실제 본 폴더만 인정한다
                path, note = dm.group(1), dm.group(2).strip(" -—:·")
                seen.add((path, 0, 0))
                verified = path in session.dirs
                reason = "ok" if verified else "도구로 확인하지 않은 폴더"
                c = Citation("code", path, 0, 0, note, verified, reason, f"{self.repo.web_base}/tree/{commit}/{path}")
                (ok if verified else bad).append(c)
                self.store.log_citation(query_id, "code", path, 0, 0, verified, reason)
        return ok, bad

    def _ref_ok(self, ref: str, session: ToolSession) -> bool:
        """흐름도 노드의 근거(파일:줄)가 인덱스에 있고 도구로 실제 본 구간인지."""
        m = CITATION_RE.search(ref)
        if not m:
            return False
        path, start = m.group(1), int(m.group(2))
        end = int(m.group(3) or start)
        if not (self.store.file_info(self.repo.name, path) or self.store.doc_file(path)):
            return False
        return session.covers(path, min(start, end), max(start, end))

    # ---------- 실행 ----------
    def _small_cost(self, comp) -> float:
        if not comp:
            return 0.0
        return estimate_cost(comp.model, comp.input_tokens, comp.output_tokens,
                             comp.cache_read_tokens, comp.cache_write_tokens) or 0.0

    async def _scope(self, question: str, forced_domains: list[str] | None, has_image: bool = False) -> tuple[Scope, float]:
        scope_cost = 0.0
        image_kw = {"has_image": True} if has_image else {}  # 이미지가 없으면 기존 시그니처 그대로 부른다
        if forced_domains is not None:
            scope = Scope(kind="multi_hop", domains=forced_domains, confidence="high", forced=True, reason="사용자가 범위 선택")
            if self.scoper:
                s2, comp = await self.scoper.scope(question, **image_kw)
                scope.kind, scope.keywords, scope_cost = s2.kind if s2.kind not in ("broad", "out_of_scope", "unsupported", "external") else "multi_hop", s2.keywords, self._small_cost(comp)
                scope.external = s2.external
        elif self.scoper:
            scope, comp = await self.scoper.scope(question, **image_kw)
            scope_cost = self._small_cost(comp)
        else:
            scope = Scope(kind="multi_hop", confidence="low", reason="스코핑 미사용")
        return apply_screenshot_rule(scope, question, has_image), scope_cost

    def _external(self, scope: Scope) -> tuple[str | None, int]:
        """스코핑의 외부 조사 부분을 붙여넣기용 프롬프트로. 개인정보·비밀값 거절(out_of_scope)에는 만들지 않는다."""
        if not scope.external or scope.kind in ("out_of_scope", "unsupported"):
            return None, 0
        terms = internal_terms(self.store.structure_names(self.repo.name), (self.repo.name, *self.repo.external_blocklist))
        ep = build_external_prompt(scope.external, self.repo.public_summary, terms)
        if not ep:
            return None, 0
        if ep.dropped:
            scope.reason += f" · 외부 조사 {len(ep.dropped)}개 항목 제외(내부 식별자)"
        return ep.text or None, len(ep.dropped)

    async def answer(self, question: str, source: str = "cli", user_id: str | None = None,
                     channel: str | None = None, thread_ts: str | None = None,
                     forced_domains: list[str] | None = None, extra_context: str = "",
                     screenshot: ScreenCapture | None = None) -> Answer:
        """screenshot: 첨부 이미지에서 읽은 문구·배치(이미지 자체는 받지 않는다). 추출 비용은 질문 비용에 합산한다."""
        commit = self.store.get_repo_commit(self.repo.name) or "unknown"
        query_id = self.store.log_query(question, source, user_id, channel, thread_ts, commit)
        has_image = screenshot is not None  # 문구를 하나도 못 읽어도 이미지가 왔으면 첨부로 본다
        vision_cost = screenshot.cost_usd if screenshot is not None else 0.0

        # 1) 스코핑
        try:
            scope, scope_cost = await self._scope(question, forced_domains, has_image)
        except LLMUnavailable as exc:
            self.store.log_result(query_id, QueryResult(status="error", answer=UNAVAILABLE_TEXT, confidence="low",
                                                        model=self.models.get("small"), error=str(exc), route="unavailable",
                                                        cost_usd=vision_cost, provider=self.provider))
            return Answer(query_id, UNAVAILABLE_TEXT, [], "low", commit, status="error", route="unavailable", error=str(exc),
                          cost_usd=vision_cost, vision_cost_usd=vision_cost, provider=self.provider)
        scope_cost += vision_cost
        route = route_for(scope) if (self.scoper or forced_domains is not None) else Route("agent", "large", self.max_turns)
        comparison = route.name == "wireframe" and has_image
        external_prompt, external_dropped = self._external(scope)

        # 2) 거절·되묻기는 에이전트 없이 끝낸다
        if route.name in ("refuse", "clarify"):
            if route.name == "refuse":
                external_only = EXTERNAL_ONLY_TEXT if external_prompt else EXTERNAL_ONLY_NO_PROMPT
                body = {"unsupported": UNSUPPORTED_TEXT, "external": external_only}.get(scope.kind, REFUSE_TEXT)
                options = []
            else:
                ids = self.domain_map.clarify_options(scope, question) if self.domain_map else []
                options = [(d.id, d.name) for d in (self.domain_map.get(i) for i in ids) if d] if self.domain_map else []
                body = scope.clarify_question or "질문 범위가 넓어요. 어느 부분이 궁금한지 골라 주세요."
            self.store.log_result(query_id, QueryResult(status=route.name, answer=body, confidence=None, model=self.models.get("small"),
                                                        cost_usd=scope_cost, scope=scope.to_json(), route=route.name,
                                                        provider=self.provider))
            return Answer(query_id, body, [], "low", commit, status=route.name,
                          route="external" if scope.kind == "external" else route.name, scope=scope,
                          options=options, cost_usd=scope_cost, vision_cost_usd=vision_cost, provider=self.provider,
                          external_prompt=external_prompt, external_dropped=external_dropped)

        # 3) 에이전트
        session = ToolSession()
        if self.domain_map and scope.domains:
            session.scope_globs = self.domain_map.globs_for(scope.domains)
        system = build_system_prompt(self.repo.name, self.repo.description, self.repo.ref, commit, self._overview())
        hints = self.domain_map.glossary_hits(question) if self.domain_map else {}
        extra = ""
        if hints:
            extra += f"- 용어 힌트: {hints}\n"
        if has_image and screenshot.texts and not comparison and not extra_context:
            extra_context = screenshot_context(screenshot.texts, screenshot.description)
        if extra_context:
            extra += f"- 첨부 맥락: {extra_context}\n"
        if scope.external:
            # 외부 조사 부분은 시스템이 프롬프트로 따로 붙인다. 에이전트가 일반론으로 메우면 근거 없는 내용이 섞인다
            extra += EXTERNAL_AGENT_NOTE.format(topics="; ".join(scope.external["topics"]))
        if comparison:
            prompt = build_change_mockup_question(question, scope.domains, scope.keywords, screenshot_block(screenshot), extra)
        elif route.name == "wireframe":
            prompt = build_wireframe_question(question, scope.domains, scope.keywords, extra)
        else:
            prompt = build_question(question, scope.kind, scope.domains, scope.keywords, extra)
        resp = await self.runner.run(AgentRequest(
            prompt, system, self._tools(query_id, session), route.max_turns or self.max_turns, self.max_budget_usd,
            model=self.models.get(route.tier), effort=self.efforts.get(route.tier),
        ))
        if route.name == "simple" and resp.error and (not resp.text.strip() or resp.error in LIMIT_ERRORS):
            # 소형 모델이 턴 한도 등으로 답을 못 내면 대형 에이전트로 한 번 올린다 (도구로 본 구간은 이어서 쓴다)
            first = resp
            route = Route("agent", "large", self.max_turns)
            resp = await self.runner.run(AgentRequest(
                prompt, system, self._tools(query_id, session), route.max_turns, self.max_budget_usd,
                model=self.models.get(route.tier), effort=self.efforts.get(route.tier),
            ))
            for f in ("cost_usd", "duration_ms", "input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "num_turns"):
                setattr(resp, f, getattr(resp, f) + getattr(first, f))
            resp.tool_calls = [*first.tool_calls, *resp.tool_calls]
            scope.reason += f" · 단순 경로 실패({first.error})로 에이전트 재시도"

        # 4) 인용 검증과 확신도
        attachment, attachment_kind, flow, spec = None, None, None, None
        followups, answer_text = extract_followups(resp.text)  # 버튼으로 보내고 본문·근거에는 남기지 않는다
        text = answer_text
        if route.name == "wireframe":
            filename = wireframe_filename(question, query_id, prefix="mockup" if comparison else "wireframe")
            attachment, text, spec = _wireframe_attachment(answer_text, filename)
            attachment_kind = "wireframe" if attachment else None
        else:
            raw_flow, text = extract_flow(answer_text)  # 명세는 본문에 남기지 않는다
            if raw_flow is not None:
                try:
                    flow = parse_flow(raw_flow)
                except FlowSpecError:
                    flow = None  # 흐름도 없이 답변만 보낸다
        body, evidence = _split_evidence(text)
        verified, unverified = self._verify(query_id, evidence or answer_text, session, commit)
        if comparison:
            body = _mark_unchecked_dev_paths(body, {c.path for c in verified})
        if flow and resp.error not in LIMIT_ERRORS:
            attachment = (flow_filename(question, query_id), render_flow(flow, lambda ref: self._ref_ok(ref, session)))
            attachment_kind = "flow"
        has_code = any(c.source == "code" for c in verified)
        if resp.error in LIMIT_ERRORS:
            # 한도로 끊기면 마지막 텍스트가 중간 독백("이제 X를 확인하겠습니다")일 수 있어 답변으로 쓰지 않는다
            confidence, body, verified, followups = "low", LIMIT_TEXT, [], []
        elif route.name == "wireframe" and not attachment:
            confidence = "low"
            body = (body + "\n\n" if body else "") + WIREFRAME_MISSING
        elif comparison and not (spec and has_proposal(spec)):
            confidence = "low"
            body = (body + "\n\n" if body else "") + COMPARISON_MISSING
        elif resp.error and not body:
            confidence = "low"
            body = "답변을 만들지 못했습니다. 잠시 후 다시 시도하거나 질문 범위를 좁혀 주세요."
            followups = []
        elif has_code and not unverified:
            confidence = "high"
        elif verified:
            confidence = "medium"  # 문서 근거만 있거나 일부 인용이 검증 실패
        else:
            confidence = "low"
        if attachment_kind == "flow" and confidence == "low":
            attachment, attachment_kind = None, None  # 검증된 근거가 없는 흐름도는 올리지 않는다
        if session.widened:
            scope.reason += " · 범위 확장됨(범위 미스)"
        total_cost = resp.cost_usd + scope_cost
        self.store.log_result(query_id, QueryResult(
            status="error" if resp.error and not resp.text else "ok", answer=resp.text, confidence=confidence,
            model=resp.model, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
            cache_read_tokens=resp.cache_read_tokens, cache_write_tokens=resp.cache_write_tokens,
            cost_usd=total_cost, duration_ms=resp.duration_ms, num_turns=resp.num_turns, error=resp.error,
            scope=scope.to_json(), route=route.name, provider=self.provider,
        ))
        cited_paths = {c.path for c in verified}
        return Answer(query_id, body, verified, confidence, commit, status="answer", route=route.name, scope=scope,
                      cost_usd=total_cost, duration_ms=resp.duration_ms, error=resp.error, unverified=unverified,
                      config_dependent=bool(cited_paths & session.config_dependent), tool_calls=len(resp.tool_calls),
                      domain_names=[d.name for d in (self.domain_map.get(i) for i in scope.domains) if d] if self.domain_map else [],
                      attachment=attachment, attachment_kind=attachment_kind, followups=followups,
                      comparison=comparison, vision_cost_usd=vision_cost, provider=self.provider,
                      external_prompt=external_prompt, external_dropped=external_dropped)
