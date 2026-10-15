"""정책·흐름 답변의 로직 흐름도: 모델이 낸 짧은 흐름 명세(JSON)를 결정적으로 SVG가 든 HTML로 그린다.

비공개 코드 로직을 외부 렌더러(Mermaid 서비스 등)로 보내지 않으려고 직접 그린다.
SVG는 이 렌더러가 만든 것만 나간다. 모델이 준 값은 모두 이스케이프한 텍스트로만 쓰고, 좌표·도형은 렌더러가 정한다.
CSP는 와이어프레임과 같다(외부 접근 차단).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Callable

from askcode.pipeline.wireframe import CSP

FLOW_BLOCK_RE = re.compile(r"```flow\s*\n(.*?)```", re.S | re.I)
NODE_TYPES = {"start", "decision", "step", "result"}
MAX_NODES = 16
MAX_EDGES = 24
MAX_TEXT = 80
MAX_REF = 160
MAX_RAW = 20_000

# 배치 치수(px)
NODE_W = 200
COL_W = 240
LINE_H = 17
PAD_Y = 12
RANK_GAP = 64
MARGIN = 24
BACK_GAP = 18

FLOW_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#f3f3f3;color:#111;font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;font-size:14px}
h1{font-size:18px;margin:0 0 6px}
.sub{color:#555;font-size:12px;margin-bottom:16px}
.canvas{background:#fff;border:1px solid #ddd;border-radius:12px;padding:8px;overflow:auto;display:inline-block;max-width:100%}
svg{display:block;font-family:inherit}
.legend{margin-top:16px;border-collapse:collapse;background:#fff;font-size:13px}
.legend th,.legend td{border-bottom:1px solid #ddd;padding:8px 12px;text-align:left;vertical-align:top}
.legend th{color:#555;font-size:12px}
.legend code{font-size:12px;color:#333}
.miss{color:#999;font-size:12px}
.keys{margin-top:12px;color:#555;font-size:12px}
"""


class FlowSpecError(ValueError):
    """흐름 명세를 읽을 수 없음."""


def extract_flow(text: str) -> tuple[str | None, str]:
    """답변에서 첫 ```flow 블록을 꺼낸다. (명세 원문 또는 None, 블록을 뺀 나머지 텍스트)"""
    m = FLOW_BLOCK_RE.search(text or "")
    if not m:
        return None, text
    rest = (text[: m.start()] + text[m.end():]).strip()
    return m.group(1).strip(), re.sub(r"\n{3,}", "\n\n", rest)


def _s(v: object, limit: int) -> str:
    if v is None or isinstance(v, (dict, list)):
        return ""
    return str(v).strip()[:limit]


def parse_flow(raw: str) -> dict:
    """명세 JSON을 읽어 정규화한다: nodes[{id,type,text,ref}], edges[{from,to,label}].

    갈래가 나뉘는 노드(나가는 엣지 2개 이상)가 하나도 없으면 그릴 가치가 없어 FlowSpecError.
    (프롬프트는 분기 2개 이상일 때만 내라고 하고, 여기서는 한 개까지 받아 준다.)
    """
    try:
        spec = json.loads(raw[:MAX_RAW])
    except json.JSONDecodeError as e:
        raise FlowSpecError(f"JSON 오류: {e}") from e
    if not isinstance(spec, dict):
        raise FlowSpecError("명세가 객체가 아님")
    nodes: list[dict] = []
    seen: set[str] = set()
    for n in spec.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid, text = _s(n.get("id"), 24), _s(n.get("text"), MAX_TEXT)
        if not nid or not text or nid in seen:
            continue
        seen.add(nid)
        ntype = n.get("type") if n.get("type") in NODE_TYPES else "step"
        nodes.append({"id": nid, "type": ntype, "text": text, "ref": _s(n.get("ref"), MAX_REF)})
        if len(nodes) >= MAX_NODES:
            break
    edges: list[dict] = []
    for e in spec.get("edges") or []:
        if not isinstance(e, dict):
            continue
        a, b = _s(e.get("from"), 24), _s(e.get("to"), 24)
        if a in seen and b in seen and a != b:
            edges.append({"from": a, "to": b, "label": _s(e.get("label"), 20)})
        if len(edges) >= MAX_EDGES:
            break
    if len(nodes) < 3 or not edges:
        raise FlowSpecError("노드·연결이 부족함")
    out_deg: dict[str, int] = {}
    for e in edges:
        out_deg[e["from"]] = out_deg.get(e["from"], 0) + 1
    if not any(d >= 2 for d in out_deg.values()):
        raise FlowSpecError("분기 없음")
    return {"title": _s(spec.get("title"), MAX_TEXT), "nodes": nodes, "edges": edges}


# ── 배치 ─────────────────────────────────────────────────────────
def _char_w(ch: str) -> float:
    return 13.0 if ord(ch) >= 0x1100 else 7.4


def _wrap(text: str, width: float, max_lines: int = 4) -> list[str]:
    """글자 폭을 어림해 줄을 나눈다. 넘치면 마지막 줄을 …로 자른다."""
    lines: list[str] = []
    cur, w = "", 0.0
    for word in text.split(" "):
        piece = (" " if cur else "") + word
        pw = sum(_char_w(c) for c in piece)
        if w + pw <= width:
            cur, w = cur + piece, w + pw
            continue
        if cur:
            lines.append(cur)
            cur, w = "", 0.0
        for ch in word:  # 한 단어가 너무 길면 글자 단위로 자른다
            cw = _char_w(ch)
            if w + cw > width and cur:
                lines.append(cur)
                cur, w = "", 0.0
            cur, w = cur + ch, w + cw
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return lines or [""]


def _ranks(ids: list[str], edges: list[dict]) -> tuple[dict[str, int], set[int]]:
    """앞으로 가는 엣지 기준 최장 경로로 층을 정한다. 되돌아가는 엣지(순환)는 따로 표시한다."""
    succ: dict[str, list[tuple[int, str]]] = {i: [] for i in ids}
    for k, e in enumerate(edges):
        succ[e["from"]].append((k, e["to"]))
    back: set[int] = set()
    state: dict[str, int] = {}  # 1 방문 중, 2 완료

    def dfs(u: str) -> None:
        state[u] = 1
        for k, v in succ[u]:
            if state.get(v) == 1:
                back.add(k)
            elif v not in state:
                dfs(v)
        state[u] = 2

    for i in ids:  # 첫 노드가 시작점
        if i not in state:
            dfs(i)
    rank = {i: 0 for i in ids}
    indeg = {i: 0 for i in ids}
    for k, e in enumerate(edges):
        if k not in back:
            indeg[e["to"]] += 1
    queue = [i for i in ids if indeg[i] == 0]
    while queue:
        u = queue.pop(0)
        for k, v in succ[u]:
            if k in back:
                continue
            rank[v] = max(rank[v], rank[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    return rank, back


def _layout(flow: dict) -> tuple[dict[str, dict], int, int, set[int], set[int]]:
    nodes, edges = flow["nodes"], flow["edges"]
    ids = [n["id"] for n in nodes]
    rank, back = _ranks(ids, edges)
    layers: dict[int, list[str]] = {}
    for i in ids:
        layers.setdefault(rank[i], []).append(i)
    # 부모 위치 평균으로 한 번 정렬해 엣지 교차를 줄인다
    pos: dict[str, float] = {}
    parents: dict[str, list[str]] = {i: [] for i in ids}
    for k, e in enumerate(edges):
        if k not in back:
            parents[e["to"]].append(e["from"])
    for r in sorted(layers):
        layer = layers[r]
        if r:
            layer.sort(key=lambda i: (sum(pos[p] for p in parents[i]) / len(parents[i]) if parents[i] else 1e9,
                                      ids.index(i)))
        for j, i in enumerate(layer):
            pos[i] = j - (len(layer) - 1) / 2
    cols = max(len(v) for v in layers.values())
    width = cols * COL_W
    box: dict[str, dict] = {}
    by_id = {n["id"]: n for n in nodes}
    y = MARGIN
    for r in sorted(layers):
        heights = {}
        for i in layers[r]:
            n = by_id[i]
            lines = _wrap(n["text"], NODE_W - (56 if n["type"] == "decision" else 28))
            heights[i] = (len(lines), max(44, len(lines) * LINE_H + PAD_Y * 2 + (8 if n["type"] == "decision" else 0)))
        row_h = max(h for _, h in heights.values())
        for i in layers[r]:
            cx = MARGIN + width / 2 + pos[i] * COL_W
            h = heights[i][1]
            box[i] = {"cx": cx, "y": y + (row_h - h) / 2, "h": h, "w": NODE_W,
                      "lines": _wrap(by_id[i]["text"], NODE_W - (56 if by_id[i]["type"] == "decision" else 28))}
        y += row_h + RANK_GAP
    # 되돌아가는 엣지와 두 층 이상 건너뛰는 엣지는 노드를 가로지르지 않게 오른쪽 바깥 통로로 돌린다
    side = back | {k for k, e in enumerate(edges) if rank[e["to"]] - rank[e["from"]] > 1}
    total_w = MARGIN * 2 + width + (len(side) * BACK_GAP + 52 if side else 0)
    return box, int(total_w), int(y - RANK_GAP + MARGIN), back, side


# ── 그리기 ───────────────────────────────────────────────────────
def _e(v: str) -> str:
    return html_lib.escape(v, quote=True)


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _shape(n: dict, b: dict, num: int) -> str:
    x0, y0, w, h = b["cx"] - b["w"] / 2, b["y"], b["w"], b["h"]
    t = n["type"]
    if t == "decision":
        k = 18
        pts = [(x0 + k, y0), (x0 + w - k, y0), (x0 + w, y0 + h / 2), (x0 + w - k, y0 + h), (x0 + k, y0 + h), (x0, y0 + h / 2)]
        shape = f'<polygon points="{" ".join(f"{_f(a)},{_f(c)}" for a, c in pts)}" fill="#f3f3f3" stroke="#111" stroke-width="1.5"/>'
    elif t == "start":
        shape = f'<rect x="{_f(x0)}" y="{_f(y0)}" width="{w}" height="{_f(h)}" rx="{_f(h / 2)}" fill="#111"/>'
    elif t == "result":
        shape = f'<rect x="{_f(x0)}" y="{_f(y0)}" width="{w}" height="{_f(h)}" rx="10" fill="#fff" stroke="#111" stroke-width="2.5"/>'
    else:
        shape = f'<rect x="{_f(x0)}" y="{_f(y0)}" width="{w}" height="{_f(h)}" rx="4" fill="#fff" stroke="#555" stroke-width="1"/>'
    color = "#fff" if t == "start" else "#111"
    weight = ' font-weight="700"' if t in ("start", "result") else ""
    ty = y0 + h / 2 - (len(b["lines"]) - 1) * LINE_H / 2 + 4.5
    text = "".join(f'<tspan x="{_f(b["cx"])}" y="{_f(ty + i * LINE_H)}">{_e(line)}</tspan>' for i, line in enumerate(b["lines"]))
    badge = (f'<circle cx="{_f(x0 + 2)}" cy="{_f(y0 + 2)}" r="9" fill="#555"/>'
             f'<text x="{_f(x0 + 2)}" y="{_f(y0 + 5.5)}" font-size="10" fill="#fff" text-anchor="middle">{num}</text>')
    return f'<g>{shape}<text font-size="13" fill="{color}" text-anchor="middle"{weight}>{text}</text>{badge}</g>'


def _label(x: float, y: float, label: str) -> str:
    if not label:
        return ""
    w = sum(_char_w(c) for c in label) * 0.85 + 10
    return (f'<rect x="{_f(x - w / 2)}" y="{_f(y - 9)}" width="{_f(w)}" height="18" rx="9" fill="#fff" stroke="#ddd"/>'
            f'<text x="{_f(x)}" y="{_f(y + 4)}" font-size="11" fill="#333" text-anchor="middle">{_e(label)}</text>')


def _edges(flow: dict, box: dict[str, dict], back: set[int], side: set[int], right: float) -> str:
    out: list[str] = []
    labels: list[str] = []
    routed = [k for k, e in enumerate(flow["edges"])
              if k in side or box[e["to"]]["y"] <= box[e["from"]]["y"]]
    # 위에서 출발하는 엣지일수록 바깥 통로를 써서 통로끼리 교차하지 않게 한다
    routed.sort(key=lambda k: (box[flow["edges"][k]["from"]]["y"], k))
    lanes = {k: len(routed) - i for i, k in enumerate(routed)}
    for k, e in enumerate(flow["edges"]):
        a, b = box[e["from"]], box[e["to"]]
        if k in lanes:
            # 오른쪽 바깥 통로로 돌아 대상 노드 오른쪽으로 들어간다. 되돌아가는 엣지는 점선
            sx, sy = a["cx"] + a["w"] / 2, a["y"] + a["h"] / 2
            tx, ty = b["cx"] + b["w"] / 2, b["y"] + b["h"] / 2
            xr = right + lanes[k] * BACK_GAP
            dash = ' stroke-dasharray="4 3"' if k in back or b["y"] <= a["y"] else ""
            out.append(f'<path d="M{_f(sx)},{_f(sy)} H{_f(xr)} V{_f(ty)} H{_f(tx + 7)}" fill="none" stroke="#555" '
                       f'stroke-width="1.3"{dash}/>')
            out.append(f'<polygon points="{_f(tx)},{_f(ty)} {_f(tx + 8)},{_f(ty - 4.5)} {_f(tx + 8)},{_f(ty + 4.5)}" fill="#555"/>')
            labels.append(_label(sx + 26, sy, e["label"]))  # 출발 노드 바로 오른쪽
            continue
        sx, sy = a["cx"], a["y"] + a["h"]
        tx, ty = b["cx"], b["y"]
        my = (sy + ty) / 2
        out.append(f'<path d="M{_f(sx)},{_f(sy)} C{_f(sx)},{_f(my)} {_f(tx)},{_f(my)} {_f(tx)},{_f(ty - 7)}" fill="none" '
                   f'stroke="#555" stroke-width="1.3"/>')
        out.append(f'<polygon points="{_f(tx)},{_f(ty)} {_f(tx - 4.5)},{_f(ty - 8)} {_f(tx + 4.5)},{_f(ty - 8)}" fill="#555"/>')
        # 라벨은 출발점 가까이(베지어 t=0.3)에 둔다
        t = 0.3
        lx = (1 - t) ** 3 * sx + 3 * (1 - t) ** 2 * t * sx + 3 * (1 - t) * t ** 2 * tx + t ** 3 * tx
        ly = (1 - t) ** 3 * sy + 3 * (1 - t) ** 2 * t * my + 3 * (1 - t) * t ** 2 * my + t ** 3 * (ty - 7)
        labels.append(_label(lx, ly, e["label"]))
    return "".join(out) + "".join(labels)  # 라벨을 선 위에 그린다


def render_flow(flow: dict, ref_ok: Callable[[str], bool] | None = None) -> str:
    """흐름 명세 → 완결된 HTML 문서(인라인 SVG + 근거 표). ref_ok가 주어지면 근거를 검증 표시한다."""
    box, width, height, back, side = _layout(flow)
    right = max(b["cx"] + b["w"] / 2 for b in box.values()) + 40  # 라벨 자리를 비운다
    nums = {n["id"]: i for i, n in enumerate(flow["nodes"], 1)}
    shapes = "".join(_shape(n, box[n["id"]], nums[n["id"]]) for n in flow["nodes"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'role="img" aria-label="로직 흐름도">{_edges(flow, box, back, side, right)}{shapes}</svg>')
    rows = []
    for n in flow["nodes"]:
        ref = n["ref"]
        if not ref:
            cell = '<span class="miss">근거 없음</span>'
        elif ref_ok is not None and not ref_ok(ref):
            cell = f'<code>{_e(ref)}</code> <span class="miss">(확인 필요)</span>'
        else:
            cell = f"<code>{_e(ref)}</code>"
        rows.append(f"<tr><td>{nums[n['id']]}</td><td>{_e(n['text'])}</td><td>{cell}</td></tr>")
    title = _e(flow["title"]) or "로직 흐름도"
    return ("<!DOCTYPE html>\n<html lang=\"ko\"><head>"
            f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{CSP}">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title}</title><style>{FLOW_CSS}</style></head><body>"
            f'<h1>{title}</h1><div class="sub">AskCode가 코드를 읽고 정리한 흐름이에요. 번호별 근거는 아래 표에 있어요.</div>'
            f'<div class="canvas">{svg}</div>'
            '<div class="keys">검은 알약 = 시작 · 육각형 = 조건 · 얇은 상자 = 처리 · 굵은 상자 = 사용자가 겪는 결과 · 점선 = 앞 단계로 돌아감</div>'
            f'<table class="legend"><thead><tr><th>#</th><th>단계</th><th>근거</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
            "</body></html>")


def flow_filename(question: str, query_id: str) -> str:
    slug = re.sub(r"[^\w가-힣]+", "-", question).strip("-")[:30] or "flow"
    return f"flow-{slug}-{query_id[:6]}.html"
