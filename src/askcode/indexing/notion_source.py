"""노션 수집기. 봇 전용 내부 통합 토큰으로, 공유받은 최상위 페이지와 그 하위 페이지만 읽는다."""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)
API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_PAGES = 200


def _rich(rt: list[dict]) -> str:
    return "".join(x.get("plain_text", "") for x in rt or [])


def _slug(title: str, page_id: str) -> str:
    s = re.sub(r"[^\w가-힣-]+", "-", title).strip("-")[:60] or "page"
    return f"notion/{s}-{page_id.replace('-', '')[-8:]}.md"


def block_to_md(block: dict) -> str | None:
    t = block.get("type")
    data = block.get(t, {}) if t else {}
    text = _rich(data.get("rich_text", []))
    mapping = {
        "paragraph": text,
        "heading_1": f"# {text}",
        "heading_2": f"## {text}",
        "heading_3": f"### {text}",
        "bulleted_list_item": f"- {text}",
        "numbered_list_item": f"1. {text}",
        "to_do": f"- [{'x' if data.get('checked') else ' '}] {text}",
        "quote": f"> {text}",
        "callout": f"> {text}",
        "toggle": f"- {text}",
        "code": f"```\n{text}\n```",
        "divider": "---",
        "table_row": " | ".join(_rich(c) for c in data.get("cells", [])),
    }
    if t in mapping:
        return mapping[t]
    if t == "child_page":
        return f"(하위 페이지: {data.get('title', '')})"
    return None


class NotionSource:
    def __init__(self, token: str, root_page_id: str, client: httpx.Client | None = None):
        self.root = root_page_id
        self.http = client or httpx.Client(
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}, timeout=30
        )

    def _children(self, block_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = self.http.get(f"{API}/blocks/{block_id}/children", params=params)
            r.raise_for_status()
            data = r.json()
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def _page_meta(self, page_id: str) -> tuple[str, str, str]:
        r = self.http.get(f"{API}/pages/{page_id}")
        r.raise_for_status()
        p = r.json()
        title = "제목 없음"
        for prop in p.get("properties", {}).values():
            if prop.get("type") == "title":
                title = _rich(prop.get("title", [])) or title
        return title, p.get("url", ""), p.get("last_edited_time", "")

    def _render(self, block_id: str, depth: int = 0, child_pages: list[str] | None = None) -> list[str]:
        lines: list[str] = []
        for b in self._children(block_id):
            md = block_to_md(b)
            if md is not None:
                lines.append(("  " * depth) + md if b.get("type", "").endswith("list_item") or b.get("type") == "to_do" else md)
            if b.get("type") == "child_page":
                if child_pages is not None:
                    child_pages.append(b["id"])
            elif b.get("has_children") and b.get("type") not in ("child_database",):
                lines.extend(self._render(b["id"], depth + 1, child_pages))
        return lines

    def collect(self) -> list[dict]:
        items, queue, seen = [], [self.root], set()
        while queue and len(items) < MAX_PAGES:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            title, url, edited = self._page_meta(pid)
            children: list[str] = []
            body = self._render(pid, 0, children)
            queue.extend(children)
            markdown = f"# {title}\n\n" + "\n".join(body)
            items.append({"path": _slug(title, pid), "title": title, "url": url, "updated_at": edited, "markdown": markdown})
        log.info("notion: %d pages", len(items))
        return items
