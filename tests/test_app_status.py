import json
import time
from types import SimpleNamespace

from askcode import app_status, host
from askcode.store.db import QueryResult, Store


def test_collect_status_for_menubar(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    st = Store(data_dir / "askcode.sqlite3")
    qid = st.log_query("앱 하단 탭에는 어떤 메뉴들이 있어요?", "slack", "U1", "D1", "1.0", "c1")
    st.log_result(qid, QueryResult(status="ok", answer="**4개예요.**", confidence="high", route="simple", cost_usd=0.003))
    low = st.log_query("화면 몇 개야?", "slack", "U1", "D1", "1.0", "c1")
    st.log_result(low, QueryResult(status="ok", answer="몰라요", confidence="low", route="agent", cost_usd=0.05))
    ev = st.log_query("평가 질문", "eval", None, None, None, "c1")
    st.log_result(ev, QueryResult(status="ok", answer="x", confidence="high", route="agent", cost_usd=0.04))
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "askcode-20261016-000000.sqlite3").write_bytes(b"x")
    monkeypatch.setattr(host, "job_states", lambda log_dir: {
        "bot": {"state": "running", "pid": 1, "last_exit": None, "last_log": ""},
        "console": {"state": "off", "pid": None, "last_exit": None, "last_log": ""},
        "backup": {"state": "waiting", "pid": None, "last_exit": None, "last_log": ""}})
    s = SimpleNamespace(data_dir=data_dir, db_path=data_dir / "askcode.sqlite3", backup_dir=backups,
                        backup_upload_cmd=None, anthropic_api_key="k")
    out = app_status.collect(s, "law-watch", "https://github.com/x/law-watch")
    json.dumps(out, ensure_ascii=False)  # 앱이 읽는 JSON으로 직렬화된다
    assert out["installed"] and out["auth"] == "api_key" and "claude" not in out  # 기본은 Claude 호출 없음
    assert out["today"] == {"questions": 2, "cost": 0.093}  # 질문 수는 슬랙만, 비용은 평가 포함 전체
    assert out["check"] == 1 and [r["question"] for r in out["recent"]][0] == "화면 몇 개야?"
    assert out["recent"][0]["flags"] == ["low"] and out["recent"][1]["route"] == "빠른 답"
    assert out["last_backup"]["name"] == "askcode-20261016-000000.sqlite3" and out["last_backup"]["offsite"] is False
    assert out["generated_at"] <= time.time()
