from pathlib import Path

from askcode.agent.base import AgentResponse
from askcode.pipeline.answer import AnswerService
from askcode.pipeline.scoping import Domain, DomainMap, Scope, load_domain_map, parse_scope, route_for
from askcode.slack.blocks import answer_blocks

DMAP = DomainMap(
    domains=[
        Domain("bills", "의안", "의안 상세", ("의안",), ("apps/api/src/bills/**",)),
        Domain("web", "웹", "웹 화면", ("웹",), ("apps/web/**",)),
    ],
    shared=("packages/**",),
    glossary={"의안": ["bill"]},
)


def test_parse_scope_filters_unknown_domains():
    s = parse_scope('결과: {"kind":"simple","domains":["bills","nope"],"confidence":"high","options":["x"]}', {"bills", "web"})
    assert s.kind == "simple" and s.domains == ["bills"] and s.options == []


def test_route_rules_are_code_enforced():
    assert route_for(Scope(kind="out_of_scope")).name == "refuse"
    assert route_for(Scope(kind="broad", options=["bills", "web"])).name == "clarify"
    assert route_for(Scope(kind="policy", confidence="low", options=["bills", "web"])).name == "clarify"
    assert route_for(Scope(kind="simple", confidence="high")).name == "simple"
    assert route_for(Scope(kind="multi_hop", confidence="high")).name == "agent"
    assert route_for(Scope(kind="broad", forced=True)).name == "agent"


def test_domain_globs_include_shared():
    globs = DMAP.globs_for(["bills"])
    assert "packages/**" in globs and "apps/api/src/bills/**" in globs
    assert DMAP.globs_for([]) is None


def test_real_domain_config_loads():
    m = load_domain_map(Path(__file__).parent.parent / "config")
    assert len(m.domains) >= 10 and m.get("follows-push") and "의안" in m.glossary


class FakeScoper:
    def __init__(self, scope):
        self._scope = scope

    async def scope(self, question):
        return self._scope, None


class ScopedRunner:
    def __init__(self):
        self.calls = []

    async def run(self, request):
        tools = {t.name: t for t in request.tools}
        self.calls.append(request)
        out_scope = await tools["read_code"].handler({"path": "apps/web/lib/bridge.ts", "start": 1, "end": 3})
        widened = await tools["widen_scope"].handler({"reason": "웹도 봐야 함"})
        again = await tools["widen_scope"].handler({"reason": "한 번 더"})
        in_scope = await tools["read_code"].handler({"path": "apps/web/lib/bridge.ts", "start": 1, "end": 3})
        assert "범위 밖" in out_scope and "넓혔습니다" in widened and "한 번만" in again and "READY" in in_scope
        return AgentResponse(text="답\n\n근거:\n- apps/web/lib/bridge.ts:1-3 — 메시지 정의", model=request.model)


async def test_scope_hard_filter_and_single_widen(store, tools, repo_cfg, repo_dir):
    runner = ScopedRunner()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools, scoper=FakeScoper(Scope(kind="multi_hop", domains=["bills"], confidence="high")),
                        domain_map=DMAP, models={"small": "s", "large": "L"})
    a = await svc.answer("의안 상세에서 웹 메시지는?")
    assert a.route == "agent" and runner.calls[0].model == "L"
    assert a.confidence == "high" and "범위 미스" in a.scope.reason
    assert "용어 힌트" in runner.calls[0].question


async def test_clarify_and_refuse_skip_agent(store, tools, repo_cfg, repo_dir):
    runner = ScopedRunner()
    clar = AnswerService(store, repo_cfg, repo_dir, runner, tools, domain_map=DMAP,
                         scoper=FakeScoper(Scope(kind="broad", options=["bills", "web"], clarify_question="어느 쪽이요?")))
    a = await clar.answer("전부 알려줘")
    assert a.status == "clarify" and a.options == [("bills", "의안"), ("web", "웹")] and not runner.calls
    blocks = answer_blocks(a)
    assert blocks[-1]["elements"][-1]["value"] == f"{a.query_id}|*"

    ref = AnswerService(store, repo_cfg, repo_dir, runner, tools, domain_map=DMAP, scoper=FakeScoper(Scope(kind="out_of_scope")))
    r = await ref.answer("가입자 이메일 뽑아줘")
    assert r.status == "refuse" and not runner.calls
    row = store.execute("SELECT route FROM query_results WHERE query_id=?", (r.query_id,)).fetchone()
    assert row["route"] == "refuse"


async def test_forced_domain_after_clarify_uses_simple_route(store, tools, repo_cfg, repo_dir):
    runner = ScopedRunner()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools, domain_map=DMAP,
                        scoper=FakeScoper(Scope(kind="simple", domains=["web"], confidence="high")), models={"small": "s", "large": "L"})
    a = await svc.answer("q", forced_domains=["bills"])
    assert a.scope.forced and a.scope.domains == ["bills"] and runner.calls[0].model == "s"


class CreditlessLLM:
    async def complete(self, *args, **kwargs):
        import anthropic
        import httpx

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API.",
            response=httpx.Response(400, request=req), body=None)


async def test_fatal_api_error_skips_agent(store, tools, repo_cfg, repo_dir):
    from askcode.pipeline.scoping import Scoper

    runner = ScopedRunner()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools, scoper=Scoper(CreditlessLLM(), DMAP), domain_map=DMAP)
    a = await svc.answer("앱 하단 탭 메뉴는?")
    assert a.status == "error" and a.route == "unavailable" and not runner.calls
    assert "credit balance" in a.error and answer_blocks(a)[0]["text"]["text"].startswith("⚠️")
    forced = await svc.answer("알림 로직", forced_domains=["bills"])
    assert forced.status == "error" and not runner.calls


def test_small_tier_disables_thinking():
    from askcode.llm.complete import _thinking

    assert _thinking("small") == {"thinking": {"type": "disabled"}}
    assert _thinking("large") == {}


def test_clarify_options_backfilled_from_terms():
    m = load_domain_map(Path(__file__).parent.parent / "config")
    ids = m.clarify_options(Scope(kind="broad", domains=["follows-push"], options=["follows-push"]), "알림 로직 알려줘")
    assert ids[0] == "follows-push" and len(ids) >= 2 and "admin-ops" in ids
    many = Scope(kind="broad", options=["bills", "people", "nope"])
    assert m.clarify_options(many, "알림") == ["bills", "people"]


async def test_simple_route_failure_escalates_to_agent(store, tools, repo_cfg, repo_dir):
    class FlakySmall:
        def __init__(self):
            self.models = []

        async def run(self, request):
            self.models.append(request.model)
            if request.model == "s":
                return AgentResponse(text="", model="s", error="error_max_turns", cost_usd=0.01, tool_calls=["code_search"])
            return AgentResponse(text="답\n\n근거:\n- apps/api/src/bills/bills.controller.ts:1-2 — x", model="L", cost_usd=0.05)

    runner = FlakySmall()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools, domain_map=DMAP, models={"small": "s", "large": "L"},
                        scoper=FakeScoper(Scope(kind="simple", domains=["bills"], confidence="high")))
    a = await svc.answer("의안 상세는?")
    assert runner.models == ["s", "L"] and a.route == "agent" and a.body == "답"
    assert abs(a.cost_usd - 0.06) < 1e-9 and a.tool_calls == 1 and "에이전트 재시도" in a.scope.reason


def test_cost_is_computed_from_tokens():
    from askcode.llm.pricing import estimate_cost

    # 슬랙 실측: 앱 하단 탭 질문의 에이전트 토큰 (Console 로그와 일치)
    cost = estimate_cost("claude-haiku-5-5", 6, 1049, cache_read_tokens=23198, cache_write_tokens=11524)
    assert abs(cost - 0.0022) < 0.0001
    assert estimate_cost("claude-sonnet-5-5", 1_000_000, 0) == 2.0
    assert estimate_cost("unknown-model", 1, 1) is None


async def test_budget_stop_does_not_leak_partial_text(store, tools, repo_cfg, repo_dir):
    class OverBudget:
        async def run(self, request):
            return AgentResponse(text="이제 서비스 코드를 확인하겠습니다.", model="L", error="error_max_budget_usd", cost_usd=0.5)

    svc = AnswerService(store, repo_cfg, repo_dir, OverBudget(), tools, domain_map=DMAP, models={"small": "s", "large": "L"},
                        scoper=FakeScoper(Scope(kind="multi_hop", domains=["bills"], confidence="high")))
    a = await svc.answer("의안 흐름 전부")
    assert a.confidence == "low" and "탐색 한도" in a.body and "확인하겠습니다" not in a.body and not a.citations


async def test_escalation_sums_tokens_and_handles_partial_text(store, tools, repo_cfg, repo_dir):
    class Partial:
        def __init__(self):
            self.models = []

        async def run(self, request):
            self.models.append(request.model)
            if request.model == "s":
                return AgentResponse(text="찾는 중…", model="s", error="error_max_turns", input_tokens=10, output_tokens=5)
            return AgentResponse(text="답\n\n근거:\n- apps/api/src/bills/bills.controller.ts:1-2 — x", model="L",
                                 input_tokens=20, output_tokens=7)

    runner = Partial()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools, domain_map=DMAP, models={"small": "s", "large": "L"},
                        scoper=FakeScoper(Scope(kind="simple", domains=["bills"], confidence="high")))
    a = await svc.answer("의안 상세는?")
    row = store.execute("SELECT input_tokens, output_tokens FROM query_results WHERE query_id=?", (a.query_id,)).fetchone()
    assert runner.models == ["s", "L"] and a.body == "답" and tuple(row) == (30, 12)


async def test_effort_follows_route_tier(store, tools, repo_cfg, repo_dir):
    runner = ScopedRunner()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools, domain_map=DMAP, models={"small": "s", "large": "L"},
                        efforts={"large": "medium"},
                        scoper=FakeScoper(Scope(kind="multi_hop", domains=["bills"], confidence="high")))
    await svc.answer("의안 상세에서 웹 메시지는?")
    assert runner.calls[0].effort == "medium"


async def test_scoper_retries_once_on_broken_json():
    from askcode.llm.complete import Completion
    from askcode.pipeline.scoping import Scoper

    class TwoShot:
        def __init__(self):
            self.n = 0

        async def complete(self, prompt, tier="small", cache_prefix=None, max_tokens=800):
            self.n += 1
            text = '{"kind": "multi_hop", "external": {"topics": ["a" "b"]}}' if self.n == 1 else '{"kind": "simple", "domains": ["bills"]}'
            return Completion(text, "haiku", 10, 20, 0, 0)

    llm = TwoShot()
    scope, comp = await Scoper(llm, DMAP).scope("의안 상세는?")
    assert llm.n == 2 and scope.kind == "simple" and comp.output_tokens == 40
