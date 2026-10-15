import json

import pytest

from askcode.agent.base import AgentResponse
from askcode.pipeline.answer import AnswerService
from askcode.pipeline.flowchart import FlowSpecError, extract_flow, flow_filename, parse_flow, render_flow
from askcode.pipeline.prompts import build_question
from askcode.pipeline.scoping import Scope

REF = "apps/api/src/bills/bills.controller.ts:1-6"
FLOW = {
    "title": "매일 받기 푸시 조건",
    "nodes": [
        {"id": "s", "type": "start", "text": "푸시 작업이 돈다", "ref": REF},
        {"id": "d1", "type": "decision", "text": "매일 받기로 설정했나?", "ref": REF},
        {"id": "d2", "type": "decision", "text": "새 소식이 있나?", "ref": "apps/web/없는파일.ts:3"},
        {"id": "r1", "type": "result", "text": "오전 8시에 모아서 푸시"},
        {"id": "r2", "type": "result", "text": "즉시 푸시"},
        {"id": "r3", "type": "result", "text": "보내지 않음"},
    ],
    "edges": [
        {"from": "s", "to": "d1"}, {"from": "d1", "to": "d2", "label": "예"}, {"from": "d1", "to": "r2", "label": "아니오"},
        {"from": "d2", "to": "r1", "label": "예"}, {"from": "d2", "to": "r3", "label": "아니오"},
        {"from": "r3", "to": "s", "label": "다음 날"},  # 되돌아가는 엣지
        {"from": "s", "to": "없는노드"},
    ],
}


def test_render_flow_draws_svg_nodes_edges_and_refs():
    out = render_flow(parse_flow(json.dumps(FLOW, ensure_ascii=False)), ref_ok=lambda r: r == REF)
    assert out.startswith("<!DOCTYPE html>") and "default-src 'none'" in out
    assert out.count("<svg") == 1 and out.count("<polygon points") >= 2  # 조건 육각형 2개 + 화살촉
    assert "매일 받기로 설정했나?" in out and ">예<" in out and ">아니오<" in out
    assert 'stroke-dasharray' in out  # 되돌아가는 엣지는 점선
    assert out.count("<tr><td>") == 6 and f"<code>{REF}</code></td>" in out
    assert "없는파일.ts:3</code> <span class=\"miss\">(확인 필요)" in out and "근거 없음" in out
    assert "<script" not in out.lower() and "url(" not in out and "https:" not in out


def test_render_flow_is_deterministic():
    raw = json.dumps(FLOW, ensure_ascii=False)
    assert render_flow(parse_flow(raw)) == render_flow(parse_flow(raw))


def test_render_flow_escapes_model_text():
    evil = '</text><script>alert(1)</script><foreignObject><img src=x onerror=alert(1)>"\''
    spec = {"title": evil, "nodes": [{"id": "a", "type": "start", "text": evil, "ref": evil},
                                     {"id": "b", "type": "decision", "text": evil},
                                     {"id": "c", "text": evil}, {"id": "d", "text": evil}],
            "edges": [{"from": "a", "to": "b", "label": evil}, {"from": "b", "to": "c", "label": evil},
                      {"from": "b", "to": "d", "label": evil}]}
    out = render_flow(parse_flow(json.dumps(spec)))
    low = out.lower()
    assert "<script" not in low and "<img" not in low and "<foreignobject" not in low
    assert "&lt;script&gt;" in out


@pytest.mark.parametrize("raw", [
    "{not json", "[]", '{"nodes": [], "edges": []}',
    # 분기 없는 일직선 흐름은 그리지 않는다
    json.dumps({"nodes": [{"id": i, "text": i} for i in "abc"], "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]}),
])
def test_parse_flow_rejects_broken_or_trivial_specs(raw):
    with pytest.raises(FlowSpecError):
        parse_flow(raw)


def test_extract_flow_and_filename():
    raw, rest = extract_flow('**결론**\n- 조건\n\n```flow\n{"nodes": []}\n```\n\n근거:\n- a/b.ts:1 — x')
    assert raw == '{"nodes": []}' and "```" not in rest and rest.startswith("**결론**") and "근거:" in rest
    assert extract_flow("흐름 없음") == (None, "흐름 없음")
    assert flow_filename("매일 받기로 설정하면 푸시가 언제 와요?", "abcdef12") == "flow-매일-받기로-설정하면-푸시가-언제-와요-abcdef.html"


def test_flow_instruction_only_for_policy_questions():
    assert "```flow" in build_question("q", "policy", [], []) and "```flow" in build_question("q", "multi_hop", [], [])
    assert "```flow" not in build_question("q", "simple", [], []) and "{{" not in build_question("q", "policy", [], [])


class FakeScoper:
    async def scope(self, question):
        return Scope(kind="policy", confidence="high"), None


class FlowRunner:
    def __init__(self, block: str):
        self.block = block
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        tools = {t.name: t for t in request.tools}
        await tools["read_code"].handler({"path": "apps/api/src/bills/bills.controller.ts", "start": 1, "end": 6})
        return AgentResponse(text="**매일 받기면 아침에 모아서 와요.**\n- 조건 1\n\n" + self.block +
                                  f"근거:\n- {REF} — 푸시 조건", model="L")


FLOW_OUT = "```flow\n" + json.dumps(FLOW, ensure_ascii=False) + "\n```\n\n"


async def test_policy_answer_attaches_flow(store, tools, repo_cfg, repo_dir):
    runner = FlowRunner(FLOW_OUT)
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools, scoper=FakeScoper())
    a = await svc.answer("매일 받기로 설정하면 푸시가 언제 와요?")
    assert "```flow" in runner.requests[0].question
    assert a.attachment_kind == "flow" and a.confidence == "high"
    name, html = a.attachment
    assert name.startswith("flow-") and "<svg" in html and f"<code>{REF}</code></td>" in html
    assert "(확인 필요)" in html  # 도구로 보지 않은 노드 근거
    assert "```" not in a.body and "nodes" not in a.body and a.citations


@pytest.mark.parametrize("block", ["", "```flow\n{\"nodes\": [ 깨진\n```\n\n"])
async def test_policy_answer_without_valid_flow_sends_answer_only(store, tools, repo_cfg, repo_dir, block):
    svc = AnswerService(store, repo_cfg, repo_dir, FlowRunner(block), tools, scoper=FakeScoper())
    a = await svc.answer("매일 받기로 설정하면 푸시가 언제 와요?")
    assert a.attachment is None and a.attachment_kind is None and a.confidence == "high"
    assert "깨진" not in a.body and "```" not in a.body
