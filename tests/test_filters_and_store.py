import sqlite3

import pytest

from askcode.indexing.filters import PathFilter, matches
from askcode.store.db import QueryResult


def test_secret_files_always_blocked():
    f = PathFilter(exclude=())
    assert not f.allowed(".env")
    assert not f.allowed("apps/api/.env")
    assert not f.allowed("apps/web/.env.local")
    assert not f.allowed("secrets/github-app.pem")
    assert f.allowed("apps/api/.env.example")


def test_path_traversal_blocked():
    f = PathFilter(exclude=())
    assert not f.allowed("../etc/passwd")
    assert not f.allowed("apps/../../x.ts")
    assert not f.allowed("/etc/passwd")


def test_glob_semantics():
    assert matches("a/b/c.png", ["**/*.{png,jpg}"])
    assert matches("c.png", ["**/*.png"])  # **/ 는 루트도 포함
    assert matches("archive/x/y.ts", ["archive/**"])
    assert not matches("src/archive.ts", ["archive/**"])
    assert matches("apps/worker/scripts/a-results.json", ["apps/worker/scripts/*-results.json"])


def test_audit_log_is_append_only(store):
    qid = store.log_query("질문", "cli")
    store.log_result(qid, QueryResult(status="ok", answer="답"))
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE queries SET question='변조' WHERE id=?", (qid,))
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("DELETE FROM query_results WHERE query_id=?", (qid,))


def test_env_file_api_key_wins_over_shell(tmp_path, monkeypatch):
    from askcode.settings import Settings

    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\nASKCODE_AGENT_MAX_TURNS=5\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-shell-key")
    monkeypatch.setenv("ASKCODE_AGENT_MAX_TURNS", "9")
    s = Settings()
    assert s.anthropic_api_key.get_secret_value() == "from-dotenv"
    assert s.agent_max_turns == 9  # ASKCODE_* 는 환경변수가 우선(실험용 덮어쓰기 유지)


def test_shell_base_url_is_ignored_unless_in_env_file(tmp_path, monkeypatch):
    from askcode.settings import DEFAULT_ANTHROPIC_BASE_URL, Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\n", encoding="utf-8")
    assert Settings().anthropic_base_url == DEFAULT_ANTHROPIC_BASE_URL
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\nANTHROPIC_BASE_URL=https://gw.example\n", encoding="utf-8")
    assert Settings().anthropic_base_url == "https://gw.example"


def test_agent_env_overrides_inherited_claude_auth():
    from askcode.agent.claude_runner import ClaudeAgentRunner

    env = ClaudeAgentRunner("m", "bot-key", "https://api.anthropic.com")._env()
    assert env == {"ANTHROPIC_API_KEY": "bot-key", "ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}


def test_preflight_reports_rejected_key(monkeypatch):
    import anthropic
    import httpx

    from askcode.doctor import claude_preflight
    from askcode.settings import Settings

    def reject(self, **kw):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=req), body=None)

    monkeypatch.setattr(anthropic.resources.Messages, "create", reject)
    # .env에 ASKCODE_LLM_PROVIDER가 있을 수 있어 이 테스트는 api 모드를 명시한다
    msg = claude_preflight(Settings(ANTHROPIC_API_KEY="bad", llm_provider="api"))
    assert msg and "401" in msg and "bad" not in msg
