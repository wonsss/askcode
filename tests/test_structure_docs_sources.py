from pathlib import Path

from askcode.indexing.docs_index import chunk_markdown, store_virtual_docs
from askcode.indexing.notion_source import block_to_md
from askcode.indexing.structure import (
    StructureConfig, build_structure, extract_bridge, link_api, extract_client_endpoints, extract_controllers,
    extract_manifest, normalize_path,
)
from askcode.pipeline.screenshot import screenshot_context
from askcode.tools.code_tools import ToolSession
from askcode.tools.doc_tools import DocTools
from askcode.tools.structure_tools import StructureTools, _pattern_matches

CLIENT = """const V1 = "/api/v1";
export function createEndpoints(client: ApiClient) {
  return {
    getBill: (slug: string, signal?: AbortSignal) =>
      client.get<Paginated<Bill>>(`${V1}/bills/${encodeURIComponent(slug)}`, undefined, signal),
    followTarget: (body: X) =>
      client.post<Follow>(`${V1}/follows`, body),
  };
}
"""
CONTROLLER = """import { Controller, Get } from "@nestjs/common";
export class BillQueryDto {}
@Controller("bills")
export class BillsController {
  @Get(":slug")
  async findOne(@Param("slug") slug: string) {
    return this.svc.one(slug)
  }
}
"""
MANIFEST = """export const ROUTES = [
  {
    id: "bill-detail",
    sourcePatterns: ["/bills/:slug/:section?"],
    presentation: "embedded",
    targetPresentation: "native",
  },
];
"""
BRIDGE_WEB = 'export const READY = JSON.stringify({\n  type: "law-watch:content-ready",\n})\nexport function f(){ post(READY) }\n'
BRIDGE_APP = 'if (msg === READY) {}\nif (t === "law-watch:content-ready") {}\n'


def _write(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_structure_extract_and_link(tmp_path, store):
    _write(tmp_path, "packages/api-client/src/endpoints.ts", CLIENT)
    _write(tmp_path, "apps/api/src/bills/presentation/bills.controller.ts", CONTROLLER)
    _write(tmp_path, "apps/mobile/src/navigation/public-route-manifest.ts", MANIFEST)
    _write(tmp_path, "apps/web/lib/webview-bridge.ts", BRIDGE_WEB)
    _write(tmp_path, "apps/mobile/src/web.ts", BRIDGE_APP)
    _write(tmp_path, "apps/web/app/(public)/bills/[slug]/page.tsx", "export default function P(){ api.getBill(x) }")
    _write(tmp_path, "apps/mobile/app/bills/[slug].tsx", "x")
    cfg = StructureConfig()
    clients = extract_client_endpoints(tmp_path, cfg)
    assert [(c.key, c.label) for c in clients] == [("GET /api/v1/bills/:p", "getBill"), ("POST /api/v1/follows", "followTarget")]
    ctrls = extract_controllers(tmp_path, cfg)
    assert ctrls[0].label == "BillsController.findOne"  # DTO 클래스가 아니라 컨트롤러 클래스
    api = link_api(clients, ctrls)
    linked = {a.label: a.extra["controller"] for a in api if a.extra.get("client_fn")}
    assert linked == {"getBill": "BillsController.findOne", "followTarget": None}
    m = extract_manifest(tmp_path, cfg)[0]
    assert m.key == "bill-detail" and m.extra["presentation"] == "embedded" and m.extra["target"] == "native"
    files = ["apps/web/lib/webview-bridge.ts", "apps/mobile/src/web.ts"]
    sides = {(b.label, b.path) for b in extract_bridge(tmp_path, cfg, files)}
    assert ("mobile", "apps/mobile/src/web.ts") in sides and ("web", "apps/web/lib/webview-bridge.ts") in sides

    all_files = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()]
    counts = build_structure(store, "demo", tmp_path, all_files, cfg)
    assert counts["api_linked"] == 1 and counts["manifest"] == 1
    st = StructureTools(store, "demo", tmp_path)
    s = ToolSession()
    screen = st.find_screen(s, "/bills/abc")
    assert "웹뷰" in screen and "목표: 네이티브" in screen and "getBill" in screen
    assert s.covers("apps/mobile/src/navigation/public-route-manifest.ts", 2, 4)
    assert "BillsController.findOne" in st.find_api(s, "bills")


def test_path_helpers():
    assert normalize_path("/api/v1/bills/${encodeURIComponent(slug)}?x=1") == "/api/v1/bills/:p"
    assert normalize_path("/api/v1/bills/:slug") == "/api/v1/bills/:p"
    assert _pattern_matches("/bills/:slug/:section?", "/bills/abc")
    assert _pattern_matches("/bills/:slug/:section?", "/bills/abc/votes")
    assert not _pattern_matches("/bills/:slug", "/people/abc")


def test_doc_search_korean_short_terms_and_virtual_docs(store):
    md = "# 내 소식 푸시 정책\n\n## 주기\n푸시는 하루 1회 묶어서 보낸다.\n\n## 예외\n긴급 소식은 즉시 보낸다.\n"
    store_virtual_docs(store, "notion", [{"path": "notion/push-1234abcd.md", "title": "내 소식 푸시 정책",
                                          "url": "https://notion.so/x", "updated_at": "2026-10-01T00:00:00Z", "markdown": md}])
    dt = DocTools(store)
    s = ToolSession()
    out = dt.search(s, "푸시 주기")
    assert "notion/push-1234abcd.md:3-5" in out and "[노션]" in out
    assert s.covers("notion/push-1234abcd.md", 3, 5)
    read = dt.read(s, "notion/push-1234abcd.md", 6, 7)
    assert "긴급 소식" in read


def test_chunk_markdown_by_heading():
    chunks = chunk_markdown("d.md", "# T\n소개\n## A\n가\n## B\n나\n", "T")
    assert [(c["heading"], c["start_line"], c["end_line"]) for c in chunks] == [("T", 1, 2), ("A", 3, 4), ("B", 5, 6)]


def test_notion_block_conversion():
    rt = [{"plain_text": "제목"}]
    assert block_to_md({"type": "heading_2", "heading_2": {"rich_text": rt}}) == "## 제목"
    assert block_to_md({"type": "to_do", "to_do": {"rich_text": rt, "checked": True}}) == "- [x] 제목"
    assert block_to_md({"type": "image", "image": {}}) is None


def test_screenshot_context():
    ctx = screenshot_context(["팔로우", "알림 받기"], "의안 상세")
    assert '"팔로우"' in ctx and "code_search" in ctx
