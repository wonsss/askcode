from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from askcode.config import RepoConfig
from askcode.indexing.code_index import build_code_index
from askcode.store.db import Store
from askcode.tools.code_tools import CodeTools

FILES = {
    "apps/web/lib/bridge.ts": "export const READY = 'law-watch:content-ready'\nexport function send() {\n  return READY\n}\n",
    "apps/api/src/bills/bills.controller.ts": (
        "@Controller('bills')\nexport class BillsController {\n  @Get(':slug')\n  detail() {\n"
        "    // 의안 상세 조회\n    return this.service.detail()\n  }\n}\n"
    ),
    "apps/api/.env": "SECRET=do-not-read\n",
    "apps/api/.env.example": "SECRET=\n",
    "archive/old.ts": "export const OLD = 1\n",
    "assets/logo.png": "\x89PNG fake",
}


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-f", "."], cwd=root, check=True)
    return root


@pytest.fixture
def repo_cfg() -> RepoConfig:
    return RepoConfig(
        name="demo",
        url="https://github.com/example/demo",
        exclude=("**/*.png", "archive/**"),
        include_exceptions=("**/.env.example",),
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db.sqlite3")


@pytest.fixture
def tools(store: Store, repo_cfg: RepoConfig, repo_dir: Path) -> CodeTools:
    build_code_index(store, repo_cfg, repo_dir)
    store.set_repo_state(repo_cfg.name, "abc1234def")
    return CodeTools(store, repo_cfg, repo_dir)
