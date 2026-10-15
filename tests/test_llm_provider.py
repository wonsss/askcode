import json
from types import SimpleNamespace

import pytest

from askcode.llm.claude_cli import CliError, CliLLM, _result
from askcode.llm.complete import Completer, LLM
from askcode.pipeline.scoping import is_fatal_api_error
from askcode.settings import Settings, update_env_file


def test_update_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# 주석\nANTHROPIC_API_KEY=sk-1\n\n# 아래는 보존\nASKCODE_MODEL_SMALL=haiku\n")
    update_env_file(env, "ASKCODE_LLM_PROVIDER", "subscription")
    text = env.read_text()
    assert "ASKCODE_LLM_PROVIDER=subscription" in text and "sk-1" in text and "# 아래는 보존" in text
    update_env_file(env, "ASKCODE_LLM_PROVIDER", "api")
    lines = [l for l in env.read_text().splitlines() if l.startswith("ASKCODE_LLM_PROVIDER")]
    assert lines == ["ASKCODE_LLM_PROVIDER=api"]


def test_update_env_file_creates(tmp_path):
    env = tmp_path / ".env"
    update_env_file(env, "ASKCODE_LLM_PROVIDER", "subscription")
    assert env.read_text().strip() == "ASKCODE_LLM_PROVIDER=subscription"


def test_provider_setting(monkeypatch):
    monkeypatch.setenv("ASKCODE_LLM_PROVIDER", "subscription")
    assert Settings().llm_provider == "subscription"
    monkeypatch.setenv("ASKCODE_LLM_PROVIDER", "api")
    assert Settings().llm_provider == "api"
    monkeypatch.setenv("ASKCODE_LLM_PROVIDER", "bogus")
    with pytest.raises(Exception):
        Settings()


def test_cli_env_clears_api_billing(monkeypatch):
    from askcode.llm.claude_cli import cli_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    env = cli_env()
    assert env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert env["ANTHROPIC_BASE_URL"] == "" and env["PATH"]


def test_result_parses_usage():
    obj = {"type": "result", "is_error": False, "result": "OK",
           "usage": {"input_tokens": 2, "output_tokens": 4,
                     "cache_read_input_tokens": 10, "cache_creation_input_tokens": 20}}
    c = _result(obj, "m")
    assert (c.text, c.input_tokens, c.output_tokens, c.cache_read_tokens, c.cache_write_tokens) == ("OK", 2, 4, 10, 20)


def test_result_error_fatal_detection():
    with pytest.raises(CliError) as e:
        _result({"type": "result", "is_error": True, "result": "Failed to authenticate: OAuth session expired"}, "m")
    assert e.value.fatal
    with pytest.raises(CliError) as e:
        _result({"type": "result", "is_error": True, "result": "something else broke"}, "m")
    assert not e.value.fatal
    assert is_fatal_api_error(e.value) is False


def test_clillm_implements_completer(tmp_path):
    s = SimpleNamespace(model_small="claude-haiku-5-5", model_large="claude-sonnet-5-5")
    cli = CliLLM(s)
    assert isinstance(cli, Completer)
    assert isinstance(LLM(Settings()), Completer)


def test_collect_status_reports_provider(tmp_path, monkeypatch):
    from askcode import app_status, host
    from askcode.store.db import Store

    data_dir = tmp_path / "data"
    Store(data_dir / "askcode.sqlite3")
    monkeypatch.setattr(host, "job_states", lambda d: {
        n: {"state": "off", "pid": None, "last_exit": None, "last_log": ""} for n in ("bot", "console", "backup")})
    s = SimpleNamespace(data_dir=data_dir, db_path=data_dir / "askcode.sqlite3", backup_dir=tmp_path / "b",
                        backup_upload_cmd=None, anthropic_api_key="k", llm_provider="subscription")
    out = app_status.collect(s, "r", "https://x")
    assert out["provider"] == "subscription" and "claude_cli" in out
    assert out["auth"] in ("subscription", "cli_missing")
    json.dumps(out)
