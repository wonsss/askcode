from askcode.tools.code_tools import ToolSession


def test_index_excludes_secrets_binaries_and_archive(store, tools, repo_cfg):
    paths = {r["path"] for r in store.execute("SELECT path FROM files WHERE repo=?", (repo_cfg.name,))}
    assert "apps/api/.env" not in paths
    assert "apps/api/.env.example" in paths
    assert "archive/old.ts" not in paths
    assert "assets/logo.png" not in paths
    assert "apps/web/lib/bridge.ts" in paths


def test_search_finds_korean_and_marks_session(tools):
    s = ToolSession()
    out = tools.search(s, "의안 상세")
    assert "apps/api/src/bills/bills.controller.ts:5" in out
    assert s.covers("apps/api/src/bills/bills.controller.ts", 5, 5)


def test_search_never_returns_secret_file(tools):
    out = tools.search(ToolSession(), "do-not-read")
    assert "결과 없음" in out and ".env:" not in out


def test_read_clamps_and_rejects(tools):
    s = ToolSession()
    out = tools.read(s, "apps/api/src/bills/bills.controller.ts", 3, 999)
    assert "[3-8 / 8줄]" in out
    assert s.covers("apps/api/src/bills/bills.controller.ts", 4, 6)
    assert "허용되지 않은" in tools.read(s, "apps/api/.env")
    assert "인덱스에 없는" in tools.read(s, "archive/old.ts") or "허용되지 않은" in tools.read(s, "archive/old.ts")


def test_list_dir_groups_one_level(tools):
    out = tools.list_dir(ToolSession(), "apps")
    assert "apps/api/" in out and "apps/web/" in out
