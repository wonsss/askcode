"""문서 인덱스. 레포 문서·노션·슬랙을 같은 형태(경로·줄 번호가 있는 마크다운)로 저장해
코드와 똑같이 `경로:줄` 인용과 검증을 할 수 있게 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

from askcode.indexing.filters import PathFilter, matches
from askcode.store.db import Store

HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*$")
MAX_CHUNK_LINES = 60
REPO_DOC_GLOBS = ("docs/**/*.md", "specs/**/*.md", "*.md", "apps/*/README.md", "apps/*/*.md")


def chunk_markdown(path: str, text: str, title: str, url: str | None = None,
                   updated_at: str | None = None) -> list[dict]:
    """제목 단위로 자르고, 너무 긴 섹션은 MAX_CHUNK_LINES 단위로 다시 자른다."""
    lines = text.splitlines()
    sections: list[tuple[int, int, str | None]] = []
    start, heading = 1, None
    for i, line in enumerate(lines, 1):
        m = HEADING_RE.match(line)
        if m and i > start:
            sections.append((start, i - 1, heading))
            start = i
        if m:
            heading = m.group(2)
    sections.append((start, len(lines), heading))
    chunks = []
    for s, e, h in sections:
        for cs in range(s, e + 1, MAX_CHUNK_LINES):
            ce = min(e, cs + MAX_CHUNK_LINES - 1)
            body = "\n".join(lines[cs - 1: ce]).strip()
            if body:
                chunks.append({"path": path, "title": title, "heading": h, "url": url, "updated_at": updated_at,
                               "start_line": cs, "end_line": ce, "text": body})
    return chunks


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines()[:20]:
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2)
    return fallback


def index_repo_docs(store: Store, repo_path: Path, files: list[str], web_base: str, commit: str,
                    path_filter: PathFilter) -> int:
    docs, chunks = [], []
    for rel in files:
        if not rel.endswith(".md") or not matches(rel, REPO_DOC_GLOBS) or not path_filter.allowed(rel):
            continue
        text = (repo_path / rel).read_text(encoding="utf-8", errors="replace")
        title = _title_of(text, rel)
        url = f"{web_base}/blob/{commit}/{rel}"
        docs.append({"path": rel, "title": title, "url": url, "updated_at": None,
                     "lines": len(text.splitlines()), "body": text})
        chunks.extend(chunk_markdown(rel, text, title, url))
    store.replace_docs("repo", docs, chunks)
    return len(docs)


def store_virtual_docs(store: Store, source: str, items: list[dict]) -> int:
    """노션·슬랙처럼 레포 밖 문서. items: path, title, url, updated_at, markdown."""
    docs, chunks = [], []
    for it in items:
        text = it["markdown"]
        docs.append({"path": it["path"], "title": it["title"], "url": it.get("url"), "updated_at": it.get("updated_at"),
                     "lines": len(text.splitlines()), "body": text})
        chunks.extend(chunk_markdown(it["path"], text, it["title"], it.get("url"), it.get("updated_at")))
    store.replace_docs(source, docs, chunks)
    return len(docs)
