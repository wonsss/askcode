"""코드 인덱스. git 추적 파일 중 제외 규칙을 통과한 텍스트 파일만 등록한다.

인덱스에 없는 파일은 검색·읽기 도구가 거부하므로, 이 목록이 봇이 볼 수 있는 범위의 기준이다.
결정론적으로 만들어지며 토큰을 쓰지 않는다.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from askcode.config import RepoConfig
from askcode.indexing.filters import PathFilter
from askcode.store.db import Store

LANG_BY_EXT = {
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".py": "python", ".kt": "kotlin",
    ".swift": "swift", ".java": "java", ".sql": "sql", ".prisma": "prisma",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".css": "css", ".html": "html", ".sh": "shell", ".toml": "toml",
}
MAX_FILE_BYTES = 1_000_000


@dataclass
class IndexStats:
    tracked: int
    indexed: int
    excluded: int
    binary_or_large: int


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:8192]


def build_code_index(store: Store, repo: RepoConfig, repo_path: Path) -> IndexStats:
    flt = PathFilter(repo.exclude, repo.include_exceptions)
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo_path, capture_output=True, check=True).stdout
    tracked = [p for p in out.decode().split("\0") if p]
    rows: list[tuple[str, str | None, int, int]] = []
    excluded = skipped = 0
    for rel in tracked:
        if not flt.allowed(rel):
            excluded += 1
            continue
        full = repo_path / rel
        if not full.is_file() or full.is_symlink():
            skipped += 1
            continue
        size = full.stat().st_size
        if size > MAX_FILE_BYTES:
            skipped += 1
            continue
        data = full.read_bytes()
        if not _is_text(data):
            skipped += 1
            continue
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        rows.append((rel, LANG_BY_EXT.get(full.suffix.lower()), lines, size))
    store.replace_files(repo.name, rows)
    return IndexStats(tracked=len(tracked), indexed=len(rows), excluded=excluded, binary_or_large=skipped)
