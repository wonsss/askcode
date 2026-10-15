from askcode.agent.base import AgentResponse
from askcode.pipeline.answer import AnswerService, extract_followups
from askcode.slack.blocks import answer_blocks, mark_followup_picked

ANSWER = """**기본은 웹뷰예요.**
- 의안 상세는 웹 페이지를 앱 안에서 보여 줘요.

근거:
- apps/api/src/bills/bills.controller.ts:3-6 — 상세 조회

후속 질문:
- 의안 목록 화면도 웹뷰예요?
- `네이티브` 상세는 언제 켜져요?
- <후속 질문 3>
- 이 질문은 버튼에 넣기에는 너무 길어서 잘리지 않도록 빼야 하는 아주 아주 아주 아주 아주 아주 긴 후속 질문 문장입니다
"""


def test_extract_followups_strips_list_and_filters():
    items, rest = extract_followups(ANSWER)
    assert items == ["의안 목록 화면도 웹뷰예요?", "네이티브 상세는 언제 켜져요?"]
    assert "후속 질문" not in rest and rest.rstrip().endswith("상세 조회")
    assert extract_followups("후속 질문 없음") == ([], "후속 질문 없음")


def test_extract_followups_before_evidence_and_numbered():
    text = "답\n\n**후속 질문:**\n1. 매주 받기는 언제 와요?\n2) 방해금지 시간은 어떻게 돼요?\n\n근거:\n- a/b.ts:1 — x"
    items, rest = extract_followups(text)
    assert items == ["매주 받기는 언제 와요?", "방해금지 시간은 어떻게 돼요?"]
    assert rest == "답\n\n근거:\n- a/b.ts:1 — x"


class Runner:
    def __init__(self, error=None):
        self.error = error

    async def run(self, request):
        tools = {t.name: t for t in request.tools}
        await tools["read_code"].handler({"path": "apps/api/src/bills/bills.controller.ts", "start": 1, "end": 8})
        return AgentResponse(text=ANSWER, model="fake", error=self.error)


async def test_pipeline_moves_followups_to_buttons(store, tools, repo_cfg, repo_dir):
    a = await AnswerService(store, repo_cfg, repo_dir, Runner(), tools).answer("의안 상세는 웹뷰야?")
    assert a.followups == ["의안 목록 화면도 웹뷰예요?", "네이티브 상세는 언제 켜져요?"]
    assert "후속 질문" not in a.body and a.citations and a.confidence == "high"
    blocks = answer_blocks(a)
    fb = next(b for b in blocks if b.get("block_id") == "followups")
    assert [e["action_id"] for e in fb["elements"]] == ["followup_0", "followup_1"]
    assert fb["elements"][0]["value"] == f"{a.query_id}|의안 목록 화면도 웹뷰예요?"
    assert blocks[-1]["elements"][0]["action_id"] == "show_evidence"  # 기존 버튼 줄은 맨 아래 유지

    picked = mark_followup_picked(blocks, "의안 목록 화면도 웹뷰예요?")
    assert not any(b.get("block_id") in ("followups", "followups_label") for b in picked)
    assert "🙋 이어서 물어봄: 의안 목록 화면도 웹뷰예요?" in str(picked)
    assert picked[-1]["elements"][0]["action_id"] == "show_evidence"


async def test_limit_stop_drops_followups(store, tools, repo_cfg, repo_dir):
    a = await AnswerService(store, repo_cfg, repo_dir, Runner(error="error_max_budget_usd"), tools).answer("q")
    assert a.followups == [] and not any(b.get("block_id") == "followups" for b in answer_blocks(a))
