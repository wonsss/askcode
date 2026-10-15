from askcode.agent.base import AgentResponse
from askcode.evaluation import render_markdown, run_eval
from askcode.pipeline.answer import AnswerService
from askcode.pipeline.scoping import Scope


class ByQuestionScoper:
    async def scope(self, q):
        if "전부" in q:
            return Scope(kind="broad", options=[]), None
        return Scope(kind="simple", domains=[], confidence="high"), None


class Runner:
    async def run(self, request):
        tools = {t.name: t for t in request.tools}
        await tools["read_code"].handler({"path": "apps/api/src/bills/bills.controller.ts", "start": 1, "end": 8})
        return AgentResponse(text="답\n근거:\n- apps/api/src/bills/bills.controller.ts:3-6 — x", cost_usd=0.02)


async def test_run_eval_scores_behavior_and_evidence(store, tools, repo_cfg, repo_dir, tmp_path):
    svc = AnswerService(store, repo_cfg, repo_dir, Runner(), tools, scoper=ByQuestionScoper())
    golden = [
        {"id": "q1", "type": "simple", "question": "상세는?", "expected_behavior": "answer",
         "evidence": [{"path": "apps/api/src/bills/bills.controller.ts", "lines": "3-6"}, {"path": "x/y.ts", "lines": "1"}]},
        {"id": "q2", "type": "broad", "question": "전부 알려줘", "expected_behavior": "clarify"},
        {"id": "q3", "type": "out_of_scope", "question": "전부 메일 뽑아줘", "expected_behavior": "refuse"},
    ]
    results, summary, md = await run_eval(svc, None, golden, tmp_path / "out", "abc1234")
    r = {x.id: x for x in results}
    assert r["q1"].evidence_recall == 0.5 and r["q1"].correct is None
    assert r["q2"].behavior_ok and r["q2"].correct is True
    assert not r["q3"].behavior_ok and r["q3"].correct is False  # 되묻기했지만 거절이 기대
    assert summary["behavior"] == "67% (2/3)" and md.exists()
    assert "| q1 |" in render_markdown(results, summary, "abc1234")
