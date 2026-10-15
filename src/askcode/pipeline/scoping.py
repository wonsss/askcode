"""스코핑과 라우팅. 소형 모델이 질문 + 도메인 맵(프롬프트 캐시)만 보고 범위를 정한다.

라우팅 규칙은 프롬프트가 아니라 이 코드(route_for)가 강제한다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from askcode.llm.complete import LLM, Completion

log = logging.getLogger(__name__)

class LLMUnavailable(Exception):
    """크레딧 부족·인증 실패처럼 다시 시도해도 안 되는 API 오류. 에이전트로 넘기지 않고 바로 실패시킨다."""


def is_fatal_api_error(exc: Exception) -> bool:
    import anthropic

    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return True
    return isinstance(exc, anthropic.BadRequestError) and "credit balance" in str(exc).lower()


KINDS = ("simple", "policy", "screen_hybrid", "multi_hop", "decision", "broad", "out_of_scope")


@dataclass(frozen=True)
class Domain:
    id: str
    name: str
    summary: str
    terms: tuple[str, ...]
    paths: tuple[str, ...]


@dataclass
class DomainMap:
    domains: list[Domain]
    shared: tuple[str, ...]
    glossary: dict[str, list[str]] = field(default_factory=dict)

    def get(self, did: str) -> Domain | None:
        return next((d for d in self.domains if d.id == did), None)

    def globs_for(self, ids: list[str]) -> tuple[str, ...] | None:
        doms = [d for d in (self.get(i) for i in ids) if d]
        if not doms:
            return None
        globs: list[str] = list(self.shared)
        for d in doms:
            globs.extend(d.paths)
        return tuple(dict.fromkeys(globs))

    def glossary_hits(self, question: str) -> dict[str, list[str]]:
        return {k: v for k, v in self.glossary.items() if k in question}

    def prompt_block(self) -> str:
        lines = [f"- {d.id}: {d.name} — {d.summary} (용어: {', '.join(d.terms)})" for d in self.domains]
        return "\n".join(lines)


def load_domain_map(config_dir: Path) -> DomainMap:
    raw = yaml.safe_load((config_dir / "domains.yaml").read_text(encoding="utf-8"))
    domains = [Domain(d["id"], d["name"], d.get("summary", ""), tuple(d.get("terms", [])), tuple(d.get("paths", [])))
               for d in raw["domains"]]
    gpath = config_dir / "glossary.yaml"
    glossary = yaml.safe_load(gpath.read_text(encoding="utf-8")) if gpath.exists() else {}
    return DomainMap(domains, tuple(raw.get("shared", [])), glossary or {})


@dataclass
class Scope:
    kind: str = "multi_hop"
    domains: list[str] = field(default_factory=list)
    confidence: str = "low"
    clarify_question: str | None = None
    options: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    reason: str = ""
    forced: bool = False

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


SCOPE_SYSTEM = """\
당신은 사내 코드 질의응답 봇의 라우터입니다. 기획자의 질문을 보고 아래 JSON만 출력합니다(설명 금지).

{{"kind": "simple|policy|screen_hybrid|multi_hop|decision|broad|out_of_scope",
 "domains": ["도메인 id", ...],          // 관련 도메인 1~3개
 "confidence": "high|medium|low",        // 도메인 판단 확신도
 "clarify_question": "되물을 질문 또는 null",
 "options": ["도메인 id", ...],           // 되물을 때 보여줄 후보 2~5개
 "keywords": ["코드 검색에 쓸 영문 식별자·한국어 문구", ...],
 "reason": "한 줄 근거"}}

유형 기준:
- simple: 값·목록·존재 여부처럼 한두 곳만 보면 되는 질문
- policy: 조건·예외·제한이 있는 규칙 질문
- screen_hybrid: 화면이 네이티브인지 웹뷰인지, 화면 진입·웹과 앱 차이
- multi_hop: 화면 → API → 서버 로직처럼 여러 레이어를 따라가야 하는 질문
- decision: 왜 그렇게 결정했는지, 배경·미채택안 (문서 근거)
- broad: 범위가 너무 넓어 한 번에 답하면 부정확한 질문 (예: "알림 로직 알려줘", "결제 로직 전부")
- out_of_scope: 실제 사용자 개인정보·운영 DB 데이터·비밀값 요청, 코드베이스와 무관한 질문

broad이면 탐색하지 말고 clarify_question과 options를 채웁니다.

도메인 목록:
{domains}
"""


def parse_scope(text: str, valid_ids: set[str]) -> Scope:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json")
    d = json.loads(m.group(0))
    kind = d.get("kind") if d.get("kind") in KINDS else "multi_hop"
    return Scope(
        kind=kind,
        domains=[x for x in d.get("domains") or [] if x in valid_ids][:3],
        confidence=d.get("confidence") if d.get("confidence") in ("high", "medium", "low") else "low",
        clarify_question=d.get("clarify_question") or None,
        options=[x for x in d.get("options") or [] if x in valid_ids][:5],
        keywords=[str(x) for x in d.get("keywords") or []][:8],
        reason=str(d.get("reason") or "")[:200],
    )


@dataclass
class Route:
    name: str          # refuse | clarify | simple | agent
    tier: str          # small | large
    max_turns: int


def route_for(scope: Scope) -> Route:
    if scope.forced:
        return Route("agent", "large", 12) if scope.kind in ("multi_hop", "decision", "broad") else Route("simple", "small", 8)
    if scope.kind == "out_of_scope":
        return Route("refuse", "small", 0)
    if scope.kind == "broad" or (scope.confidence == "low" and len(scope.options) >= 2):
        return Route("clarify", "small", 0)
    if scope.kind in ("simple", "screen_hybrid") and scope.confidence != "low":
        return Route("simple", "small", 8)
    return Route("agent", "large", 12)


class Scoper:
    def __init__(self, llm: LLM, domain_map: DomainMap):
        self.llm = llm
        self.map = domain_map
        self._system = SCOPE_SYSTEM.format(domains=domain_map.prompt_block())

    async def scope(self, question: str) -> tuple[Scope, Completion | None]:
        hits = self.map.glossary_hits(question)
        prompt = f"질문: {question}" + (f"\n용어 힌트: {json.dumps(hits, ensure_ascii=False)}" if hits else "")
        try:
            comp = await self.llm.complete(prompt, tier="small", cache_prefix=self._system, max_tokens=400)
            scope = parse_scope(comp.text, {d.id for d in self.map.domains})
            return scope, comp
        except Exception as exc:
            if is_fatal_api_error(exc):
                # 대형 모델도 같은 이유로 실패한다. 에이전트로 넘기면 하위 프로세스 기동 비용·시간만 든다.
                raise LLMUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
            # 스코핑 실패 시 범위 제한 없이 에이전트로 처리 (답은 하되 비용이 더 든다)
            log.warning("scoping failed: %s", exc)
            return Scope(kind="multi_hop", confidence="low", reason=f"스코핑 실패: {type(exc).__name__}"), None
