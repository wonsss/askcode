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
