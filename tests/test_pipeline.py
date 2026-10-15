from askcode.agent.base import AgentRequest, AgentResponse
from askcode.agent.claude_runner import _sdk_tool
from askcode.pipeline.answer import AnswerService, _split_evidence
from askcode.slack.blocks import answer_blocks, to_mrkdwn


class FakeRunner:
    """실제 도구를 호출한 뒤, 검증 가능한 인용과 불가능한 인용을 섞어 답한다."""

    def __init__(self):
        self.request: AgentRequest | None = None

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.request = request
        tools = {t.name: t for t in request.tools}
        await tools["code_search"].handler({"query": "의안 상세"})
        await tools["read_code"].handler({"path": "apps/api/src/bills/bills.controller.ts", "start": 1, "end": 8})
        text = (
            "의안 상세는 **BillsController**의 `GET /bills/:slug`가 처리합니다.\n\n"
            "근거:\n"
            "- apps/api/src/bills/bills.controller.ts:3-6 — 상세 조회 핸들러\n"
            "- apps/web/lib/bridge.ts:1 — 읽지 않은 파일\n"
            "- apps/api/src/nope.ts:10 — 없는 파일\n"
        )
        return AgentResponse(text=text, model="fake", input_tokens=100, output_tokens=50, cost_usd=0.01, num_turns=3)


async def test_pipeline_verifies_citations_and_logs(store, tools, repo_cfg, repo_dir):
    runner = FakeRunner()
    svc = AnswerService(store, repo_cfg, repo_dir, runner, tools)
    a = await svc.answer("의안 상세는 어디서 처리돼?", source="cli")

    assert a.confidence == "medium"
    assert [c.path for c in a.citations] == ["apps/api/src/bills/bills.controller.ts"]
    assert a.citations[0].url.endswith("/blob/abc1234def/apps/api/src/bills/bills.controller.ts#L3-L6")
    reasons = {c.path: c.reason for c in a.unverified}
    assert reasons["apps/web/lib/bridge.ts"] == "도구로 확인하지 않은 구간"
    assert reasons["apps/api/src/nope.ts"] == "인덱스에 없는 파일"
    assert "근거:" not in a.body

    assert "abc1234" in runner.request.system_prompt
    assert store.execute("SELECT COUNT(*) n FROM tool_calls WHERE query_id=?", (a.query_id,)).fetchone()["n"] == 2
    assert store.execute("SELECT COUNT(*) n FROM citations WHERE query_id=?", (a.query_id,)).fetchone()["n"] == 3
    row = store.execute("SELECT confidence, cost_usd FROM query_results WHERE query_id=?", (a.query_id,)).fetchone()
    assert row["confidence"] == "medium" and row["cost_usd"] == 0.01


async def test_no_evidence_means_low_confidence(store, tools, repo_cfg, repo_dir):
    class Empty:
        async def run(self, request):
            return AgentResponse(text="잘 모르겠습니다. 확인 필요합니다.")

    a = await AnswerService(store, repo_cfg, repo_dir, Empty(), tools).answer("?")
    assert a.confidence == "low" and not a.citations


def test_split_evidence_uses_last_header():
    body, ev = _split_evidence("답\n\n**근거:**\n- a/b.ts:1")
    assert body == "답" and ev == "- a/b.ts:1"


async def test_sdk_tool_wraps_handler_and_errors():
    from askcode.agent.base import ToolSpec

    async def ok(args):
        return f"hi {args['x']}"

    async def boom(args):
        raise ValueError("bad")

    good = _sdk_tool(ToolSpec("t", "d", {"type": "object"}, ok))
    bad = _sdk_tool(ToolSpec("t2", "d", {"type": "object"}, boom))
    assert (await good.handler({"x": 1}))["content"][0]["text"] == "hi 1"
    assert "도구 오류" in (await bad.handler({}))["content"][0]["text"]


async def test_slack_blocks(store, tools, repo_cfg, repo_dir):
    a = await AnswerService(store, repo_cfg, repo_dir, FakeRunner(), tools).answer("q")
    blocks = answer_blocks(a)
    assert blocks[-1]["type"] == "actions"
    assert {e["action_id"] for e in blocks[-1]["elements"]} == {"show_evidence", "feedback_up", "feedback_down", "escalate"}
    assert "API 키" in str(blocks)  # 메타 줄의 프로바이더 라벨
    assert "github.com/example/demo/blob/abc1234def" in str(blocks)
    assert to_mrkdwn("**굵게** [링크](https://x.y)") == "*굵게* <https://x.y|링크>"
    assert to_mrkdwn("- 첫째\n  - 둘째") == "• 첫째\n  • 둘째"


def test_plain_body_keeps_code_only_in_dev_note():
    from askcode.slack.blocks import plain_body

    out = plain_body("- 웹 `/embed/bills` 화면\n🔧 개발자 참고: `POST /api/v1/follows`")
    assert out == "- 웹 /embed/bills 화면\n🔧 개발자 참고: `POST /api/v1/follows`"


def test_clean_question_strips_mention_and_copied_backticks():
    from askcode.slack.app import clean_question

    assert clean_question("<@U123ABC> `의안 상세 화면은 네이티브야 웹뷰야?`") == "의안 상세 화면은 네이티브야 웹뷰야?"
    assert clean_question("“알림 로직 알려줘”") == "알림 로직 알려줘"
    assert clean_question("`code_search`가 뭐야?") == "`code_search`가 뭐야?"


def test_citation_line_is_short_and_plain():
    from askcode.pipeline.answer import Citation
    from askcode.slack.blocks import citation_line, short_path

    c = Citation("code", "apps/mobile/src/navigation/public-route-manifest.ts", 193, 200, "`bill-detail` 화면 등록", True, "ok",
                 "https://github.com/x/y/blob/c/apps/mobile/src/navigation/public-route-manifest.ts#L193")
    line = citation_line(c)
    assert short_path(c.path) == "navigation/public-route-manifest.ts"
    assert "|navigation/public-route-manifest.ts:193-200>" in line and "`" not in line and "bill-detail 화면 등록" in line


def test_split_body_moves_footnotes_out():
    from askcode.slack.blocks import split_body

    main, foot = split_body("**결론**\n- 하나\n\n확인 필요: 운영 설정\n🔧 개발자 참고: `X`")
    assert main == "**결론**\n- 하나" and foot == ["확인 필요: 운영 설정", "🔧 개발자 참고: `X`"]


def test_evidence_notes_and_blocks():
    from askcode.pipeline.answer import evidence_notes
    from askcode.slack.blocks import evidence_blocks

    notes = evidence_notes("답\n\n근거:\n- apps/mobile/app/(tabs)/_layout.tsx:5-17 — 탭 4개 순서")
    assert notes == {("apps/mobile/app/(tabs)/_layout.tsx", 5): "탭 4개 순서"}
    cites = [{"source": "code", "path": "apps/mobile/app/(tabs)/_layout.tsx", "start": 5, "end": 17, "verified": True,
              "reason": "ok", "url": "https://x", "note": "탭 4개 순서"},
             {"source": "code", "path": "apps/a/b.tsx", "start": 1, "end": 3, "verified": False,
              "reason": "도구로 확인하지 않은 구간", "url": "https://y", "note": ""}]
    steps = [("mcp__askcode__code_search", {"query": "BottomTab", "path_glob": "apps/mobile/**"}),
             ("read_code", {"path": "apps/mobile/src/components/chrome/floating-tab-bar.tsx", "start": 1, "end": 60})]
    res = {"route": "simple", "duration_ms": 8100, "cost_usd": 0.0033, "input_tokens": 8, "cache_read_tokens": 30000,
           "cache_write_tokens": 10000, "output_tokens": 1500, "num_turns": 5, "provider": "subscription"}
    text = str(evidence_blocks(cites, steps, res))
    assert "<https://x|(tabs)/_layout.tsx:5-17>  탭 4개 순서" in text and "제외: 도구로 확인하지 않은 구간" in text
    assert "코드 검색  “BottomTab” (apps/mobile 안에서)" in text and "chrome/floating-tab-bar.tsx 1–60줄" in text
    assert "빠른 답(소형 모델)" in text and "입력 40.0k · 캐시 재사용 75%" in text
    assert "구독" in text and "비용 환산" in text  # 구독 모드는 실제 과금이 아니라 환산값
    api = str(evidence_blocks(cites, steps, {**res, "provider": "api"}))
    assert "API 키" in api and "비용 환산" not in api
