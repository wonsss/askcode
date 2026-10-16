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

from askcode.llm.complete import Completer, Completion

log = logging.getLogger(__name__)

class LLMUnavailable(Exception):
    """크레딧 부족·인증 실패처럼 다시 시도해도 안 되는 API 오류. 에이전트로 넘기지 않고 바로 실패시킨다."""


def is_fatal_api_error(exc: Exception) -> bool:
    from askcode.llm.claude_cli import CliError

    if isinstance(exc, CliError):
        return exc.fatal
    import anthropic

    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return True
    return isinstance(exc, anthropic.BadRequestError) and "credit balance" in str(exc).lower()


# 스크린샷 + 이 문형이면 변경안 목업(원본 스크린샷과 비교)으로 보낸다 (예: "공유 버튼 추가하면 어떻게 돼?")
CHANGE_RE = re.compile(
    r"(추가|넣|바꾸|바꿔|변경|빼|없애|옮기|옮겨|붙이)[가-힣]*?(면|다면)(?![가-힣])|목업|변경안|시안"
    r"|(추가|넣|보여주|보여 주|띄우|붙이|바꾸|바꿔|빼|없애)[가-힣 ]{0,6}(거|건|게|것)\s*(어때|어떨까|어떤가)"
)


def is_change_request(question: str) -> bool:
    return bool(CHANGE_RE.search(question or ""))


KINDS = ("simple", "policy", "screen_hybrid", "multi_hop", "decision", "broad", "wireframe", "unsupported",
         "out_of_scope", "external")


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

    def term_matches(self, question: str) -> list[str]:
        """질문에 도메인 용어의 단어(2자 이상)가 들어 있는 도메인. 되묻기 후보가 모자랄 때 보충용."""
        return [d.id for d in self.domains
                if any(w in question for t in d.terms for w in t.split() if len(w) >= 2)]

    def clarify_options(self, scope: "Scope", question: str) -> list[str]:
        """되묻기 후보. 모델이 1개만 주면 버튼 하나짜리 되묻기가 되므로 용어 매칭으로 2개 이상을 맞춘다."""
        ids = [i for i in dict.fromkeys([*scope.options, *scope.domains]) if self.get(i)]
        if len(ids) < 2:
            ids = list(dict.fromkeys([*ids, *self.term_matches(question)]))
        return ids[:5]

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
    external: dict | None = None  # {"goal", "topics"} — 외부 조사로 넘길 부분 (external.py가 프롬프트로 만든다)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


SCOPE_SYSTEM = """\
당신은 사내 코드 질의응답 봇의 라우터입니다. 기획자의 질문을 보고 아래 JSON만 출력합니다(설명 금지).

{{"kind": "simple|policy|screen_hybrid|multi_hop|decision|broad|wireframe|unsupported|out_of_scope",
 "domains": ["도메인 id", ...],          // 관련 도메인 1~3개
 "confidence": "high|medium|low",        // 도메인 판단 확신도
 "clarify_question": "되물을 질문 또는 null",
 "options": ["도메인 id", ...],           // 되물을 때 보여줄 후보 2~5개
 "keywords": ["코드 검색에 쓸 영문 식별자·한국어 문구", ...],
 "external": {{"goal": "외부 조사 목적 한 줄", "topics": ["외부에서 조사할 질문", ...]}} 또는 null,
 "reason": "한 줄 근거"}}

유형 기준:
- simple: 값·목록·존재 여부처럼 한두 곳만 보면 되는 질문
- policy: 조건·예외·제한이 있는 규칙 질문
- screen_hybrid: 화면이 네이티브인지 웹뷰인지, 화면 진입·웹과 앱 차이
- multi_hop: 화면 → API → 서버 로직처럼 여러 레이어를 따라가야 하는 질문
- decision: 왜 그렇게 결정했는지, 배경·미채택안 (문서 근거)
- broad: 범위가 너무 넓어 한 번에 답하면 부정확한 질문 (예: "알림 로직 알려줘", "결제 로직 전부")
- wireframe: 새 기능·화면의 와이어프레임·화면 초안·목업을 만들어 달라는 요청. domains에는 참고할 기존 화면의 도메인을 넣는다.
  화면 스크린샷이 첨부됐고 그 화면에 기능·요소를 추가·변경하면 어떻게 되는지 묻는 요청도 wireframe이다
- unsupported: 코드 수정·PR·배포·문서 작성·번역처럼 AskCode가 하지 않는 작업 요청 (와이어프레임은 제외)
- out_of_scope: 실제 사용자 개인정보·운영 DB 데이터·비밀값 요청, 서비스·기획과 무관한 질문
- external: 질문 전체가 외부 조사(타 서비스 사례·비교, 외부 API·데이터 소스·라이브러리 조사, 법·저작권 일반)일 때

"이 기능 개발 가능할까", "어떻게 만들 수 있을까" 같은 실현 가능성 질문은 broad가 아니라 multi_hop입니다
(기존 코드에서 쓸 수 있는 데이터·구조·붙일 지점을 찾는 질문).

external 필드: 질문에 외부 조사가 필요한 부분이 있으면 채우고, 없으면 null입니다.
질문 일부만 외부 조사라면 kind는 나머지(코드·문서로 답할 부분) 기준으로 정하고 external만 채웁니다.
external은 사용자가 사외 AI에 붙여넣는 글이 됩니다. 일반적인 한국어로만 쓰고 파일 경로·코드 식별자·API 경로·
내부 화면 이름·서비스 이름·미공개 정책 값은 절대 넣지 않습니다. topics는 1~5개, 각각 60자 이내 한 문장 질문입니다.
문자열 안에 큰따옴표(")를 쓰지 않습니다.

broad이면 탐색하지 말고 clarify_question과 options를 채웁니다. clarify_question에는 범위를 좁히는 질문만 쓰고
외부 조사 안내는 넣지 않습니다(따로 안내됩니다).

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
        external=_parse_external(d.get("external")),
    )


def _parse_external(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    topics = [str(t) for t in raw.get("topics") or [] if isinstance(t, (str, int, float)) and str(t).strip()]
    return {"goal": str(raw.get("goal") or ""), "topics": topics[:5]} if topics else None


@dataclass
class Route:
    name: str          # refuse | clarify | simple | agent
    tier: str          # small | large
    max_turns: int


def apply_screenshot_rule(scope: Scope, question: str, has_image: bool) -> Scope:
    """스크린샷 + 변경·추가 문형은 모델 분류와 무관하게 와이어프레임(변경안 목업)으로 보낸다. 거절 유형은 건드리지 않는다."""
    if has_image and scope.kind not in ("out_of_scope", "unsupported", "external", "wireframe") and is_change_request(question):
        scope.kind = "wireframe"
        scope.reason = (scope.reason + " · " if scope.reason else "") + "스크린샷 + 변경 요청 → 변경안 목업"
    return scope


def route_for(scope: Scope) -> Route:
    if scope.forced and scope.kind == "wireframe":
        return Route("wireframe", "large", 12)
    if scope.forced:
        return Route("agent", "large", 12) if scope.kind in ("multi_hop", "decision", "broad") else Route("simple", "small", 8)
    if scope.kind in ("out_of_scope", "unsupported", "external"):
        return Route("refuse", "small", 0)
    if scope.kind == "wireframe":
        return Route("wireframe", "large", 12)
    if scope.kind == "broad" or (scope.confidence == "low" and len(scope.options) >= 2):
        return Route("clarify", "small", 0)
    if scope.kind in ("simple", "screen_hybrid") and scope.confidence != "low":
        return Route("simple", "small", 8)
    return Route("agent", "large", 12)


# 한국어 JSON은 글자당 토큰이 많다. external까지 채우면 1,400토큰을 넘은 실측이 있어 여유를 둔다(API 모드에서 잘림 방지)
SCOPE_MAX_TOKENS = 2000


class Scoper:
    def __init__(self, llm: Completer, domain_map: DomainMap):
        self.llm = llm
        self.map = domain_map
        self._system = SCOPE_SYSTEM.format(domains=domain_map.prompt_block())

    async def scope(self, question: str, has_image: bool = False) -> tuple[Scope, Completion | None]:
        hits = self.map.glossary_hits(question)
        prompt = (f"질문: {question}" + (f"\n용어 힌트: {json.dumps(hits, ensure_ascii=False)}" if hits else "")
                  + ("\n첨부: 앱/웹 화면 스크린샷 1장" if has_image else ""))
        try:
            comp = await self.llm.complete(prompt, tier="small", cache_prefix=self._system, max_tokens=SCOPE_MAX_TOKENS)
            try:
                scope = parse_scope(comp.text, {d.id for d in self.map.domains})
            except ValueError as exc:
                # JSON이 깨지면 한 번 더 묻는다(약 $0.003). 실패해 범위 없이 대형 에이전트로 가면 수십 배가 든다
                log.warning("scope json invalid (%s), retrying once: %.200s", exc, comp.text)
                first = comp
                comp = await self.llm.complete(prompt, tier="small", cache_prefix=self._system, max_tokens=SCOPE_MAX_TOKENS)
                for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                    setattr(comp, f, getattr(comp, f) + getattr(first, f))
                scope = parse_scope(comp.text, {d.id for d in self.map.domains})
            return scope, comp
        except Exception as exc:
            if is_fatal_api_error(exc):
                # 대형 모델도 같은 이유로 실패한다. 에이전트로 넘기면 하위 프로세스 기동 비용·시간만 든다.
                raise LLMUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
            # 스코핑 실패 시 범위 제한 없이 에이전트로 처리 (답은 하되 비용이 더 든다)
            log.warning("scoping failed: %s", exc)
            return Scope(kind="multi_hop", confidence="low", reason=f"스코핑 실패: {type(exc).__name__}"), None
