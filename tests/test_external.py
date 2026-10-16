from dataclasses import replace

from askcode.agent.base import AgentResponse
from askcode.pipeline.answer import AnswerService
from askcode.pipeline.external import build_external_prompt, internal_terms, leaks
from askcode.pipeline.scoping import Scope, parse_scope, route_for
from askcode.slack.blocks import answer_blocks

TERMS = internal_terms([("GET /api/v1/representatives", "getLegislationRepresentatives"),
                        ("POST /api/v1/admin/bill-ai/runs/:p/output", "BillAiAdminController.submitOutput"),
                        ("term-party-detail", "embedded"), ("web /people/:slug/bills", "web")],
                       ("law-watch", "lawwatch"))


def test_leak_filter_catches_code_shapes_and_internal_names():
    for bad in ("apps/api/src/bills 구조를 참고한 뉴스 수집", "/api/v1/representatives 응답에 뉴스 붙이기",
                "getLegislationRepresentatives 확장 방법", "BillAiAdminController 같은 관리자 흐름",
                "term-party-detail 화면에 뉴스", "news_items 테이블 설계", "lawwatch 경쟁 서비스", "`code`",
                "representatives 데이터와 뉴스 매칭", "https://example.com 참고", "service.ts 수정"):
        assert leaks(bad, TERMS), bad
    for ok in ("국회의원 이름으로 뉴스를 검색하는 국내 뉴스 API(네이버 검색 API, 빅카인즈 등) 비교",
               "동명이인 기사를 거르는 방법", "기사 제목·요약을 앱에 보여 줄 때 저작권 이슈", "웹/앱에서 뉴스 피드 UX 사례",
               "iOS/Android 푸시 알림 빈도 권장 사례"):
        assert not leaks(ok, TERMS), ok


def test_build_prompt_drops_leaky_items_and_uses_public_summary():
    ep = build_external_prompt({"goal": "의원별 최신 뉴스 제공",
                                "topics": ["뉴스 수집 API 비교", "getLegislationRepresentatives에 붙이는 방법", "동명이인 검증 방법"]},
                               "국회 입법 기록을 보여 주는 웹·모바일 서비스", TERMS)
    assert ep.text.startswith("국회 입법 기록을 보여 주는 웹·모바일 서비스에서")
    assert "1. 뉴스 수집 API 비교" in ep.text and "2. 동명이인 검증 방법" in ep.text
    assert "getLegislation" not in ep.text and len(ep.dropped) == 1


def test_build_prompt_none_or_empty():
    assert build_external_prompt(None, "", TERMS) is None
    assert build_external_prompt({"goal": "", "topics": []}, "", TERMS) is None
    only_bad = build_external_prompt({"goal": "x", "topics": ["apps/web 구조"]}, "", TERMS)
    assert only_bad.text == "" and only_bad.dropped == ["apps/web 구조"]
    leaky_goal = build_external_prompt({"goal": "term-party-detail 개선", "topics": ["뉴스 API 비교"]}, "", TERMS)
    assert "목표" not in leaky_goal.text and leaky_goal.text.startswith("저희 서비스에서")


def test_parse_scope_reads_external_and_external_kind_refuses():
    s = parse_scope('{"kind":"multi_hop","domains":["bills"],"external":{"goal":"뉴스","topics":["뉴스 API 비교", 3, ""]}}',
                    {"bills"})
    assert s.external == {"goal": "뉴스", "topics": ["뉴스 API 비교", "3"]}
    assert parse_scope('{"kind":"simple","external":{"topics":[]}}', set()).external is None
    assert parse_scope('{"kind":"external"}', set()).kind == "external"
    assert route_for(Scope(kind="external")).name == "refuse"


class FakeScoper:
    def __init__(self, scope):
        self._scope = scope

    async def scope(self, question):
        return self._scope, None


class Runner:
    def __init__(self):
        self.calls = []

    async def run(self, request):
        self.calls.append(request)
        tools = {t.name: t for t in request.tools}
        await tools["read_code"].handler({"path": "apps/web/lib/bridge.ts", "start": 1, "end": 3})
        return AgentResponse(text="붙일 수 있어요\n\n근거:\n- apps/web/lib/bridge.ts:1-3 — 메시지", model=request.model)


EXT = {"goal": "의원별 최신 뉴스", "topics": ["국내 뉴스 API 비교", "apps/web 구조 참고"]}


async def test_mixed_question_answers_code_part_and_attaches_prompt(store, tools, repo_cfg, repo_dir):
    runner = Runner()
    cfg = replace(repo_cfg, public_summary="입법 정보 서비스")
    svc = AnswerService(store, cfg, repo_dir, runner, tools,
                        scoper=FakeScoper(Scope(kind="multi_hop", confidence="high", external=dict(EXT))))
    a = await svc.answer("의원 최신 뉴스 개발 가능할까? 외부 리서치도")
    assert a.status == "answer" and runner.calls
    q = runner.calls[0].question
    assert "외부 조사 항목" in q and "국내 뉴스 API 비교" in q and "일반론" in q
    assert a.external_prompt.startswith("입법 정보 서비스에서") and "apps/web" not in a.external_prompt
    assert a.external_dropped == 1 and "외부 조사 1개 항목 제외" in a.scope.reason
    texts = [b["text"]["text"] for b in answer_blocks(a) if b["type"] == "section"]
    assert any(t.startswith("```") and "국내 뉴스 API 비교" in t for t in texts)
    row = store.execute("SELECT scope FROM query_results WHERE query_id=?", (a.query_id,)).fetchone()
    assert "국내 뉴스 API 비교" in row["scope"]


async def test_external_only_skips_agent(store, tools, repo_cfg, repo_dir):
    runner = Runner()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools,
                        scoper=FakeScoper(Scope(kind="external", external={"goal": "", "topics": ["유사 서비스 사례"]})))
    a = await svc.answer("다른 서비스들은 의원 뉴스 어떻게 보여줘?")
    assert a.status == "refuse" and a.route == "external" and not runner.calls
    blocks = answer_blocks(a)
    assert blocks[0]["text"]["text"].startswith("🌐") and "```" in blocks[-1]["text"]["text"]


async def test_no_prompt_for_personal_data_refusal(store, tools, repo_cfg, repo_dir):
    svc = AnswerService(store, repo_cfg, repo_dir, Runner(), tools,
                        scoper=FakeScoper(Scope(kind="out_of_scope", external={"goal": "", "topics": ["사용자 이메일 조회"]})))
    a = await svc.answer("가입자 이메일 뽑아줘")
    assert a.status == "refuse" and a.external_prompt is None
    assert answer_blocks(a)[0]["text"]["text"].startswith("🚫")


async def test_clarify_keeps_prompt(store, tools, repo_cfg, repo_dir):
    svc = AnswerService(store, repo_cfg, repo_dir, Runner(), tools,
                        scoper=FakeScoper(Scope(kind="broad", clarify_question="어느 쪽?", external={"goal": "", "topics": ["뉴스 API 비교"]})))
    a = await svc.answer("뉴스 기능 전부")
    assert a.status == "clarify" and "뉴스 API 비교" in a.external_prompt
    assert any("```" in b.get("text", {}).get("text", "") for b in answer_blocks(a))
