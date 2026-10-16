import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from askcode.console.data import ConsoleData, compare_runs
from askcode.console.server import create_app
from askcode.slack.blocks import PUBLIC_NOTE, thinking_blocks
from askcode.store.db import QueryResult, Store

ANSWER = """**매일 받기 푸시는 한국 시간 오전 9시 이후에 가요.**
- 기획서에는 오전 8시로 적혀 있어요. ⚠️ 불일치

근거:
- apps/worker/src/notifications/mobile-push-window.ts:10-20 — 09:00 판단

후속 질문:
- 매주 받기는 언제 와요?
"""


@pytest.fixture
def seeded(tmp_path):
    db = tmp_path / "askcode.sqlite3"
    st = Store(db)
    st.set_repo_state("law-watch", "c1")
    st.record_commit_files("law-watch", "c1", [("apps/worker/src/notifications/mobile-push-window.ts", "b1"),
                                               ("apps/mobile/app/(tabs)/_layout.tsx", "t1")])
    scope = json.dumps({"kind": "policy", "domains": ["follows-push"], "confidence": "high", "reason": "푸시 정책"})

    def add(question, source="slack", answer=ANSWER, route="agent", confidence="high", commit="c1", cite=True, **kw):
        qid = st.log_query(question, source, "U1", "C1", "1.0", commit)
        st.log_result(qid, QueryResult(status=kw.get("status", "ok"), answer=answer, confidence=confidence,
                                       model="claude-sonnet-5-5", input_tokens=10, output_tokens=200,
                                       cache_read_tokens=900, cache_write_tokens=100, cost_usd=0.04,
                                       duration_ms=15000, num_turns=5, error=kw.get("error"), scope=scope,
                                       route=route, provider=kw.get("provider", "api")))
        if cite:
            st.log_citation(qid, "code", "apps/worker/src/notifications/mobile-push-window.ts", 10, 20, True, "ok")
            st.log_citation(qid, "code", "apps/a/b.ts", 1, 3, False, "도구로 확인하지 않은 구간")
        st.log_tool_call(qid, "mcp__askcode__code_search", {"query": "daily"}, "1건")
        return qid

    ids = {
        "push": add("매일 받기로 설정하면 푸시가 아침 몇 시에 와요?"),
        "sub1": add("구독은 한 사람이 몇 개까지 할 수 있어요?", answer="**최대 100개예요.**\n\n근거:\n- a/b.ts:1 — x",
                    provider="subscription"),
        "sub2": add("구독은 최대 몇 개까지 가능해요?", answer="**코드는 100개, 기획서는 50개예요.**"),
        "eval": add("평가용 질문 푸시 시각", source="eval"),
        "low": add("<script>alert(1)</script> 화면 몇 개야?", answer="**<b>위험</b> 31개 안팎이에요.**",
                   confidence="low", cite=False),
        "limit": add("한도 질문", answer="탐색 한도", confidence="low", error="error_max_budget_usd", cite=False),
    }
    st.log_feedback(ids["sub2"], "U2", "down")
    st.log_feedback(ids["push"], "U2", "up")
    res = tmp_path / "results"
    res.mkdir()
    for name, correct in (("eval-20261015-171742", False), ("eval-20261015-173540", True)):
        (res / f"{name}.json").write_text(json.dumps({
            "summary": {"items": 1, "correct": "100%" if correct else "0%", "total_cost_usd": 0.1},
            "results": [{"id": "q01", "question": "하단 탭", "correct": correct, "judge_reason": "이유",
                         "route": "simple", "cost_usd": 0.003}]}), encoding="utf-8")
    data = ConsoleData(db, "law-watch", "https://github.com/x/law-watch", res, {"follows-push": "팔로우·푸시"})
    return st, data, ids


def test_search_finds_paraphrases_and_hides_eval_by_default(seeded):
    _, data, ids = seeded
    found = [r.id for r in data.search("구독 최대")]
    assert set(found[:2]) == {ids["sub1"], ids["sub2"]}  # 질문엔 '최대'가 없어도 답에 있으면 찾는다
    assert ids["eval"] not in [r.id for r in data.search("푸시 시각")]
    assert ids["eval"] in [r.id for r in data.search("푸시 시각", source="all")]
    assert [r.id for r in data.search("", feedback="down")] == [ids["sub2"]]
    assert all("follows-push" in r.domains for r in data.search("", domain="follows-push"))


def test_freshness_by_commit_snapshots(seeded):
    st, data, ids = seeded
    row = data.get(ids["push"])
    cites = data.citations(row)
    assert data.freshness(row, cites).state == "ok"
    # 새 커밋에서 근거 파일이 바뀜
    st.set_repo_state("law-watch", "c2")
    st.record_commit_files("law-watch", "c2", [("apps/worker/src/notifications/mobile-push-window.ts", "b2"),
                                               ("apps/mobile/app/(tabs)/_layout.tsx", "t1")])
    f = data.freshness(row, cites)
    assert f.state == "changed" and f.changed == ["apps/worker/src/notifications/mobile-push-window.ts"]
    # 근거가 없으면 비교 불가
    assert data.freshness(data.get(ids["low"]), []).state == "unknown"
    # 스냅숏이 없는 커밋이면 비교 불가
    st.set_repo_state("law-watch", "c3")
    assert data.freshness(row, cites).state == "unknown"


def test_needs_check_reasons(seeded):
    _, data, ids = seeded
    reasons = {r.id: r.check_reasons for r in data.needs_check()}
    assert reasons[ids["sub2"]] == ["down"] and "low" in reasons[ids["low"]] and "limit" in reasons[ids["limit"]]
    assert ids["push"] not in reasons


def test_console_is_read_only(seeded):
    _, data, _ = seeded
    with pytest.raises(sqlite3.OperationalError):
        data.conn.execute("INSERT INTO feedback(query_id, ts, user_id, value) VALUES ('x', 0, 'u', 'up')")


def test_pages_escape_and_secure(seeded):
    _, data, ids = seeded
    client = TestClient(create_app(data))
    home = client.get("/")
    assert home.status_code == 200 and "default-src 'none'" in home.headers["content-security-policy"]
    assert home.headers["referrer-policy"] == "no-referrer"
    low = client.get(f"/q/{ids['low']}").text
    assert "<script>alert(1)</script>" not in low and "&lt;script&gt;" in low and "<b>위험</b>" not in low
    push = client.get(f"/q/{ids['push']}").text
    assert "✅ 근거 코드 그대로" in push and "매주 받기는 언제 와요?" in push and "후속 질문" not in push
    assert "mobile-push-window.ts:10-20" in push and "apps/a/b.ts" not in push  # 기획자 보기는 검증된 근거만
    ops = client.get(f"/q/{ids['push']}?view=ops").text
    assert "제외: 도구로 확인하지 않은 구간" in ops and "코드 검색" in ops and "캐시 재사용 89%" in ops
    sub_ops = client.get(f"/q/{ids['sub1']}?view=ops").text
    assert "구독" in sub_ops and "구독이라 실제 과금 없음" in sub_ops  # 프로바이더 표시 + 비용 환산 주석
    assert client.get("/q/nope").status_code == 404
    assert "구독은 최대" in client.get("/search", params={"q": "구독 최대"}).text
    assert "확인 필요" in client.get("/check").text
    ev = client.get("/eval").text
    assert "이전과 비교" in ev
    cmp_ = client.get("/eval/compare", params={"a": "eval-20261015-171742", "b": "eval-20261015-173540"}).text
    assert "새로 맞음" in cmp_


def test_compare_runs_orders_regressions_first():
    a = {"results": [{"id": "q1", "correct": True}, {"id": "q2", "correct": False}]}
    b = {"results": [{"id": "q1", "correct": False}, {"id": "q2", "correct": True}, {"id": "q3", "correct": True}]}
    assert [(r["id"], r["change"]) for r in compare_runs(a, b)] == [("q1", "새로 틀림"), ("q2", "새로 맞음"), ("q3", "새 문항")]


def test_thinking_blocks_public_note():
    assert PUBLIC_NOTE in str(thinking_blocks("⏳", public_note=True))
    assert PUBLIC_NOTE not in str(thinking_blocks("⏳"))


def test_resolve_removes_from_check_and_reopens_on_new_feedback(seeded):
    st, data, ids = seeded
    before = data.stats(7)["check"]
    assert st.resolve(ids["sub2"], "ops", "기획서 수정 요청함")
    open_ids = {r.id for r in data.needs_check()}
    done = data.needs_check(resolved=True)
    assert ids["sub2"] not in open_ids and [r.id for r in done] == [ids["sub2"]]
    assert done[0].resolved["note"] == "기획서 수정 요청함" and data.stats(7)["check"] == before - 1
    client = TestClient(create_app(data))
    assert "처리된 건 보기 (1)" in client.get("/check").text
    assert "기획서 수정 요청함" in client.get("/check?resolved=1").text
    assert "✔︎ 처리됨" in client.get(f"/q/{ids['sub2']}").text
    # 처리 뒤 새 👎가 오면 다시 열린다
    st.log_feedback(ids["sub2"], "U3", "down")
    assert ids["sub2"] in {r.id for r in data.needs_check()}
    # 다시 처리 → 명시적으로 다시 열기
    st.resolve(ids["sub2"], "ops")
    assert ids["sub2"] not in {r.id for r in data.needs_check()}
    st.resolve(ids["sub2"], "ops", reopen=True)
    assert ids["sub2"] in {r.id for r in data.needs_check()}
    assert not st.resolve("nope", "ops")
    with pytest.raises(sqlite3.IntegrityError):
        st.execute("DELETE FROM resolutions")
