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


def test_golden_expected_domains_exist():
    from pathlib import Path

    import yaml

    root = Path(__file__).parent.parent
    ids = {d["id"] for d in yaml.safe_load((root / "config/domains.yaml").read_text(encoding="utf-8"))["domains"]}
    for item in yaml.safe_load((root / "eval/golden.yaml").read_text(encoding="utf-8")):
        assert set(item.get("expected_domains") or []) <= ids, item["id"]


async def test_truncated_judge_is_not_counted_as_wrong():
    from askcode.evaluation import judge
    from askcode.llm.complete import Completion
    from askcode.pipeline.answer import Answer

    class Truncated:
        async def complete(self, *a, **k):
            return Completion('{"pass": false, "reason": "길게 이어지다 잘', "m", 1, 1, 0, 0)

    ok, reason = await judge(Truncated(), {"question": "q", "answer": "a"}, Answer("id", "답", [], "low", "c"))
    assert ok is None and "판정 실패" in reason
