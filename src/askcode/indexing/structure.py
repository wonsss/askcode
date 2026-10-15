"""코드 구조 맵 (결정론적, 토큰 0).

- api: api-client 함수 ↔ NestJS 컨트롤러 핸들러 (HTTP 메서드 + 경로로 연결)
- route: Next.js 페이지, Expo 라우트
- manifest: 모바일 public-route-manifest의 화면별 presentation(native·embedded·external·web-only)
- bridge: WebView ↔ 네이티브 메시지 타입별 송신·수신 위치

law-watch 구조에 맞춘 추출기이며, 경로는 config/repos.yaml의 structure 설정으로 바꿀 수 있다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from askcode.store.db import Store

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass
class StructureConfig:
    api_client_files: tuple[str, ...] = ("packages/api-client/src/endpoints.ts",)
    api_prefix: str = "/api/v1"
    controller_glob: str = "apps/api/src/**/*.controller.ts"
    web_app_dir: str = "apps/web/app"
    mobile_app_dir: str = "apps/mobile/app"
    route_manifest: str = "apps/mobile/src/navigation/public-route-manifest.ts"
    bridge_prefix: str = "law-watch:"
    bridge_dirs: tuple[str, ...] = ("apps/web", "apps/mobile", "packages")


@dataclass
class StructureEntry:
    kind: str
    key: str
    label: str
    path: str
    line: int
    extra: dict = field(default_factory=dict)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def normalize_path(path: str) -> str:
    """`/bills/${encodeURIComponent(slug)}` · `/bills/:slug` → `/bills/:p` 형태로 비교용 정규화."""
    path = re.sub(r"\$\{[^}]*\}", ":p", path)
    path = path.split("?")[0]
    path = re.sub(r":[A-Za-z_]\w*", ":p", path)
    path = re.sub(r"/+", "/", path).rstrip("/")
    return path or "/"


def _paths_match(a: str, b: str) -> bool:
    sa, sb = a.strip("/").split("/"), b.strip("/").split("/")
    if len(sa) != len(sb):
        return False
    return all(x == y or x == ":p" or y == ":p" for x, y in zip(sa, sb))


# --- api-client ---------------------------------------------------------------
_CLIENT_CALL_RE = re.compile(
    r"client\.(get|post|put|patch|delete)\b(?:<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>)?\(\s*`\$\{V1\}([^`]*)`", re.S
)
_FN_RE = re.compile(r"^\s{2,6}(\w+)\s*:\s*(?:async\s*)?\(", re.M)


def extract_client_endpoints(root: Path, cfg: StructureConfig) -> list[StructureEntry]:
    out: list[StructureEntry] = []
    for rel in cfg.api_client_files:
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        fns = [(m.start(), m.group(1)) for m in _FN_RE.finditer(text)]
        for m in _CLIENT_CALL_RE.finditer(text):
            fn = next((name for pos, name in reversed(fns) if pos < m.start()), "?")
            method = m.group(1).upper()
            raw = cfg.api_prefix + m.group(2)
            out.append(StructureEntry("client", f"{method} {normalize_path(raw)}", fn, rel, _line_of(text, m.start()),
                                      {"method": method, "raw_path": raw}))
    return out


# --- NestJS controllers -------------------------------------------------------
_CTRL_RE = re.compile(r"@Controller\(\s*(?:[\"'`]([^\"'`]*)[\"'`])?[^)]*\)")
_ROUTE_RE = re.compile(r"@(Get|Post|Put|Patch|Delete)\(\s*(?:[\"'`]([^\"'`]*)[\"'`])?[^)]*\)")
_HANDLER_RE = re.compile(r"^\s*(?:public\s+|private\s+)?(?:async\s+)?(\w+)\s*\(", re.M)
_CLASS_RE = re.compile(r"export\s+class\s+(\w+)")


def extract_controllers(root: Path, cfg: StructureConfig) -> list[StructureEntry]:
    out: list[StructureEntry] = []
    for p in sorted(root.glob(cfg.controller_glob)):
        rel = str(p.relative_to(root))
        text = p.read_text(encoding="utf-8")
        cm = _CTRL_RE.search(text)
        if not cm:
            continue
        prefix = cm.group(1) or ""
        cls_m = _CLASS_RE.search(text, cm.end())
        cls = cls_m.group(1) if cls_m else "?"
        for m in _ROUTE_RE.finditer(text):
            hm = _HANDLER_RE.search(text, m.end())
            handler = hm.group(1) if hm else "?"
            method = m.group(1).upper()
            raw = "/".join(x.strip("/") for x in (cfg.api_prefix, prefix, m.group(2) or "") if x.strip("/"))
            raw = "/" + raw
            out.append(StructureEntry("controller", f"{method} {normalize_path(raw)}", f"{cls}.{handler}", rel,
                                      _line_of(text, m.start()), {"method": method, "raw_path": raw}))
    return out


def link_api(clients: list[StructureEntry], controllers: list[StructureEntry]) -> list[StructureEntry]:
    """클라이언트 호출과 컨트롤러 핸들러를 메서드+경로로 잇는다. 정확 일치 우선."""
    out: list[StructureEntry] = []
    by_key: dict[str, list[StructureEntry]] = {}
    for c in controllers:
        by_key.setdefault(c.key, []).append(c)
    for cl in clients:
        method, path = cl.key.split(" ", 1)
        match = by_key.get(cl.key) or [c for c in controllers if c.extra["method"] == method
                                       and _paths_match(path, c.key.split(" ", 1)[1])]
        ctrl = match[0] if match else None
        out.append(StructureEntry(
            "api", cl.key, cl.label, cl.path, cl.line,
            {"method": method, "raw_path": cl.extra["raw_path"], "client_fn": cl.label,
             "controller": ctrl.label if ctrl else None, "controller_path": ctrl.path if ctrl else None,
             "controller_line": ctrl.line if ctrl else None},
        ))
    linked = {e.extra["controller_path"] + str(e.extra["controller_line"]) for e in out if e.extra["controller"]}
    for c in controllers:  # 클라이언트가 부르지 않는 서버 경로(관리자·내부용 등)도 남긴다
        if c.path + str(c.line) not in linked:
            out.append(StructureEntry("api", c.key, c.label, c.path, c.line,
                                      {"method": c.extra["method"], "raw_path": c.extra["raw_path"], "client_fn": None,
                                       "controller": c.label, "controller_path": c.path, "controller_line": c.line}))
    return out


# --- 라우트 -------------------------------------------------------------------
def _route_from_file(rel_parts: list[str]) -> str:
    segs = []
    for s in rel_parts:
        if s.startswith("(") and s.endswith(")"):
            continue  # 라우트 그룹
        s = re.sub(r"^\[\.\.\.(\w+)\]$", r":\1*", s)
        s = re.sub(r"^\[\[?(\w+)\]?\]$", r":\1", s)
        segs.append(s)
    return "/" + "/".join(segs)


def extract_routes(root: Path, cfg: StructureConfig) -> list[StructureEntry]:
    out: list[StructureEntry] = []
    web = root / cfg.web_app_dir
    for p in sorted(web.rglob("page.tsx")) if web.is_dir() else []:
        parts = list(p.relative_to(web).parts[:-1])
        out.append(StructureEntry("route", "web " + _route_from_file(parts), "web", str(p.relative_to(root)), 1))
    mobile = root / cfg.mobile_app_dir
    for p in sorted(mobile.rglob("*.tsx")) if mobile.is_dir() else []:
        if p.name.startswith("_") or p.name.startswith("+"):
            continue
        parts = list(p.relative_to(mobile).parts)
        parts[-1] = p.stem
        if parts[-1] == "index":
            parts = parts[:-1]
        out.append(StructureEntry("route", "mobile " + _route_from_file(parts), "mobile", str(p.relative_to(root)), 1))
    return out


_MANIFEST_ID_RE = re.compile(r'\bid:\s*"([^"]+)"')


def extract_manifest(root: Path, cfg: StructureConfig) -> list[StructureEntry]:
    p = root / cfg.route_manifest
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8")
    out = []
    ids = list(_MANIFEST_ID_RE.finditer(text))
    for i, m in enumerate(ids):
        block = text[m.start(): ids[i + 1].start() if i + 1 < len(ids) else len(text)]
        def field_(name: str) -> str | None:
            fm = re.search(rf'\b{name}:\s*"([^"]+)"', block)
            return fm.group(1) if fm else None
        patterns = re.findall(r'"(/[^"]*)"', (re.search(r"sourcePatterns:\s*\[([^\]]*)\]", block) or [None, ""])[1])
        out.append(StructureEntry("manifest", m.group(1), field_("presentation") or "?", cfg.route_manifest,
                                  _line_of(text, m.start()),
                                  {"presentation": field_("presentation"), "target": field_("targetPresentation"),
                                   "patterns": patterns}))
    return out


# --- WebView 브리지 -----------------------------------------------------------
def extract_bridge(root: Path, cfg: StructureConfig, files: list[str]) -> list[StructureEntry]:
    lit_re = re.compile(r'["\'`](' + re.escape(cfg.bridge_prefix) + r'[a-z0-9-]+)["\'`]')
    out: list[StructureEntry] = []
    consts: dict[str, str] = {}
    texts: dict[str, list[str]] = {}
    for rel in files:
        if not rel.startswith(cfg.bridge_dirs) or not rel.endswith((".ts", ".tsx")) or ".test." in rel:
            continue
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        texts[rel] = lines
        for i, line in enumerate(lines, 1):
            for m in lit_re.finditer(line):
                msg = m.group(1)
                side = "web" if rel.startswith("apps/web") else ("mobile" if rel.startswith("apps/mobile") else "shared")
                out.append(StructureEntry("bridge", msg, side, rel, i, {"code": line.strip()[:160]}))
                # 같은 줄 또는 바로 위 줄의 상수 이름을 기억해 사용처도 찾는다
                for back in (line, lines[i - 2] if i >= 2 else ""):
                    cm = re.search(r"const\s+(\w+)\s*=", back)
                    if cm:
                        consts[cm.group(1)] = msg
                        break
    for rel, lines in texts.items():
        for i, line in enumerate(lines, 1):
            for name, msg in consts.items():
                if re.search(rf"\b{name}\b", line) and not re.search(rf"const\s+{name}\b", line):
                    side = "web" if rel.startswith("apps/web") else ("mobile" if rel.startswith("apps/mobile") else "shared")
                    out.append(StructureEntry("bridge", msg, side, rel, i, {"code": line.strip()[:160], "via": name}))
    return out


def build_structure(store: Store, repo: str, root: Path, files: list[str], cfg: StructureConfig | None = None) -> dict[str, int]:
    cfg = cfg or StructureConfig()
    api = link_api(extract_client_endpoints(root, cfg), extract_controllers(root, cfg))
    entries = [*api, *extract_routes(root, cfg), *extract_manifest(root, cfg), *extract_bridge(root, cfg, files)]
    store.replace_structure(repo, [(e.kind, e.key, e.label, e.path, e.line, json.dumps(e.extra, ensure_ascii=False))
                                   for e in entries])
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    counts["api_linked"] = sum(1 for e in api if e.extra.get("client_fn") and e.extra.get("controller"))
    return counts
