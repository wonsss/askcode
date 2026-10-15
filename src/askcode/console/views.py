"""웹 콘솔 화면. 템플릿 엔진 없이 이스케이프한 HTML 조각으로 그린다.

답변·질문은 모델·사용자 입력이라 신뢰하지 않는다. 먼저 전부 이스케이프하고, 그 위에 굵게·목록·코드만 다시 입힌다.
외부 CSS·글꼴·스크립트를 쓰지 않는다(서버가 CSP로 막는다).
"""

from __future__ import annotations

import html
import re
import time
from urllib.parse import urlencode

from askcode.console.data import CHECK_REASONS, ROUTE_KO, Freshness, Row, parse_answer
from askcode.slack.blocks import short_path, tool_step

CONF_KO = {"high": "🟢 높음", "medium": "🟡 보통", "low": "🔴 낮음"}
SOURCE_KO = {"slack": "슬랙", "cli": "CLI", "eval": "평가", "all": "전체"}
SRC_LABEL = {"code": "코드", "doc": "문서", "notion": "노션", "slack": "슬랙"}

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--card:#fff;--accent:#2f5d9c;--ok:#2e7d32;--warn:#b26a00;--bad:#b3261e;--chip:#efefea}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--fg:#ecece8;--muted:#a2a29b;--line:#2f2f2c;--card:#1f1f1d;--accent:#8db4ff;--ok:#7bc47f;--warn:#f0b35a;--bad:#ff8a80;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{border-bottom:1px solid var(--line);background:var(--card)}
.wrap{max-width:1040px;margin:0 auto;padding:0 20px}
nav{display:flex;gap:18px;align-items:center;height:52px;overflow-x:auto}nav b{margin-right:10px}nav a{color:var(--muted);white-space:nowrap}nav a.on{color:var(--fg);font-weight:600}
main{padding:24px 0 60px}h1{font-size:22px;margin:0 0 6px}h2{font-size:17px;margin:28px 0 10px}
.muted{color:var(--muted)}.small{font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.stat b{display:block;font-size:24px}
form.search{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
input,select,button{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
input[type=search]{flex:1;min-width:220px}button{background:var(--fg);color:var(--bg);border:0;cursor:pointer}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}th{color:var(--muted);font-weight:500;font-size:13px}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);font-size:12px;margin:0 4px 2px 0;white-space:nowrap}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.answer p{margin:6px 0}.answer ul{margin:6px 0;padding-left:20px}.answer code,code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:13px}
.tabs{display:flex;gap:6px;margin:16px 0 0}.tabs a{padding:6px 12px;border-radius:8px;color:var(--muted)}.tabs a.on{background:var(--chip);color:var(--fg)}
ol.steps{padding-left:22px}.foot{border-left:3px solid var(--line);padding-left:10px;color:var(--muted);font-size:14px}
.table-scroll{overflow-x:auto}
"""


def e(v: object) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def when(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def money(v: float) -> str:
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def inline(text: str) -> str:
    """이스케이프한 뒤 **굵게**, `코드`만 입힌다."""
    t = e(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return t


def md(text: str) -> str:
    """답변 본문(마크다운 일부) → HTML. 문단·글머리표·번호 목록·굵게·코드만."""
    out, items = [], []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", s)
        if m:
            items.append(inline(m.group(1)))
        elif not s:
            flush()
        else:
            flush()
            out.append(f"<p>{inline(s)}</p>")
    flush()
    return "".join(out)


def page(title: str, body: str, active: str = "") -> str:
    links = [("/", "홈", "home"), ("/search", "질문 찾기", "search"), ("/check", "확인 필요", "check"), ("/eval", "평가", "eval")]
    nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>' for href, label, key in links)
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{e(title)} · AskCode</title><style>{CSS}</style></head>'
            f'<body><header><div class="wrap"><nav><b>AskCode 콘솔</b>{nav}</nav></div></header>'
            f'<main><div class="wrap">{body}</div></main></body></html>')


def chips(row: Row, domain_names: dict[str, str]) -> str:
    c = [f'<span class="chip">{e(ROUTE_KO.get(row.route or "", row.route or "처리 중"))}</span>']
    if row.confidence and row.has_answer:
        c.append(f'<span class="chip">{CONF_KO.get(row.confidence, row.confidence)}</span>')
    c += [f'<span class="chip">{e(domain_names.get(d, d))}</span>' for d in row.domains[:3]]
    c += [f'<span class="chip bad">{e(CHECK_REASONS[r])}</span>' for r in row.check_reasons]
    if "up" in row.feedback:
        c.append('<span class="chip">👍</span>')
    return "".join(c)


def search_form(params: dict, domain_names: dict[str, str], ops: bool) -> str:
    def opt(name, value, label):
        return f'<option value="{e(value)}"{" selected" if params.get(name, "") == value else ""}>{e(label)}</option>'

    domains = opt("domain", "", "모든 도메인") + "".join(opt("domain", k, v) for k, v in domain_names.items())
    routes = opt("route", "", "모든 경로") + "".join(opt("route", k, v) for k, v in ROUTE_KO.items())
    conf = opt("confidence", "", "모든 확신도") + "".join(opt("confidence", k, v) for k, v in CONF_KO.items())
    fb = opt("feedback", "", "모든 피드백") + opt("feedback", "up", "👍") + opt("feedback", "down", "👎") + opt(
        "feedback", "escalate", "확인 요청")
    days = opt("days", "", "전체 기간") + opt("days", "7", "최근 7일") + opt("days", "30", "최근 30일")
    src = ""
    if ops:
        src = "<select name=source>" + "".join(opt("source", k, v) for k, v in SOURCE_KO.items()) + "</select>"
    return (f'<form class="search" action="/search"><input type="search" name="q" value="{e(params.get("q", ""))}" '
            f'placeholder="예: 푸시 시각, 구독 최대, 하단 탭" autofocus>'
            f"<select name=domain>{domains}</select><select name=route>{routes}</select>"
            f"<select name=confidence>{conf}</select><select name=feedback>{fb}</select>"
            f"<select name=days>{days}</select>{src}<button>찾기</button></form>")


def row_table(rows: list[Row], domain_names: dict[str, str], *, show_source: bool = False, limit: int = 200) -> str:
    if not rows:
        return '<div class="card muted">결과가 없어요.</div>'
    trs = []
    for r in rows[:limit]:
        src = f"<td>{e(SOURCE_KO.get(r.source, r.source))}</td>" if show_source else ""
        preview = parse_answer(r.answer)["body"].splitlines()[0][:90] if r.has_answer and r.answer else ""
        trs.append(f'<tr><td class="small muted">{when(r.ts)}</td>{src}<td><a href="/q/{e(r.id)}">{e(r.question)}</a>'
                   f'<div class="small muted">{inline(preview)}</div></td><td>{chips(r, domain_names)}</td>'
                   f'<td class="small">{money(r.cost)}</td></tr>')
    head = "<th>시각</th>" + ("<th>출처</th>" if show_source else "") + "<th>질문</th><th></th><th>비용</th>"
    more = f'<p class="small muted">{len(rows) - limit}건 더 있어요. 조건을 좁혀 주세요.</p>' if len(rows) > limit else ""
    return f'<div class="table-scroll"><table><tr>{head}</tr>{"".join(trs)}</table></div>{more}'


def home(stats: dict, recent: list[Row], check_count: int, domain_names: dict[str, str]) -> str:
    routes = " · ".join(f"{ROUTE_KO[k]} {v}" for k, v in stats["by_route"].items() if v)
    body = (
        "<h1>무엇이 궁금하세요?</h1><p class='muted'>슬랙에서 받은 답을 다시 찾아보세요. 같은 질문을 다시 묻지 않아도 돼요.</p>"
        + search_form({}, domain_names, ops=False)
        + '<div class="grid">'
        + f'<div class="card stat"><span class="muted small">이번 주 질문 (슬랙)</span><b>{stats["questions"]}</b>'
          f'<span class="small muted">{e(routes) or "-"}</span></div>'
        + f'<div class="card stat"><span class="muted small">이번 주 비용 (전체)</span><b>{money(stats["cost_all"])}</b>'
          f'<span class="small muted">슬랙 {money(stats["cost_slack"])} · 나머지는 평가·CLI</span></div>'
        + f'<div class="card stat"><span class="muted small">확인 필요 (최근 30일)</span><b>'
          f'<a href="/check">{check_count}</a></b><span class="small muted">낮은 확신도·👎·확인 요청·오류</span></div>'
        + "</div><h2>최근 답</h2>" + row_table(recent, domain_names)
    )
    return page("홈", body, "home")


def search_page(params: dict, rows: list[Row], domain_names: dict[str, str]) -> str:
    ops = params.get("source", "slack") != "slack" or params.get("ops") == "1"
    title = f"‘{params['q']}’ 검색 결과 {len(rows)}건" if params.get("q") else f"질문 {len(rows)}건"
    toggle = ("" if ops else f'<a class="small" href="/search?{urlencode({**params, "ops": "1"})}">평가·CLI 질문도 보기</a>')
    body = (f"<h1>{e(title)}</h1>" + search_form(params, domain_names, ops=ops) + toggle
            + row_table(rows, domain_names, show_source=ops))
    return page("질문 찾기", body, "search")


def freshness_badge(f: Freshness, latest: str | None) -> str:
    if f.state == "ok":
        return (f'<span class="ok">✅ 근거 코드 그대로</span> <span class="small muted">근거 파일 {f.checked}개가 '
                f'최신 동기화(main@{e((latest or "")[:7])})까지 바뀌지 않았어요</span>')
    if f.state == "changed":
        files = ", ".join(f"<code>{e(short_path(p))}</code>" for p in f.changed[:6])
        return (f'<span class="warn">⚠️ 다시 확인 필요</span> <span class="small muted">이 답 이후 근거 파일 '
                f"{len(f.changed)}개가 바뀌었어요: {files}</span>")
    return f'<span class="muted">❔ 최신성 비교 불가</span> <span class="small muted">{e(f.reason)}</span>'


def cite_list(cites: list[dict]) -> str:
    if not cites:
        return '<p class="muted">검증된 근거가 없어요. 개발자 확인을 권장해요.</p>'
    lis = []
    for c in cites:
        loc = (short_path(c["path"]) + "/") if not c["start"] else (
            f"{short_path(c['path'])}:{c['start']}" + (f"-{c['end']}" if c["end"] != c["start"] else ""))
        label = "폴더" if not c["start"] else SRC_LABEL.get(c["source"], c["source"])
        mark = "✅" if c["verified"] else "⚠️"
        tail = e(c["note"].replace("`", "")) if c["verified"] else f'<span class="warn">제외: {e(c["reason"])}</span>'
        lis.append(f'<li>{mark} <span class="chip">{e(label)}</span><a href="{e(c["url"])}" rel="noreferrer" '
                   f'title="{e(c["path"])}">{e(loc)}</a> {tail}</li>')
    return "<ul>" + "".join(lis) + "</ul>"


def detail(row: Row, view: str, cites: list[dict], fresh: Freshness, latest: str | None, steps: list[tuple[str, dict]],
           usage: dict, fb_events: list[dict], domain_names: dict[str, str]) -> str:
    parsed = parse_answer(row.answer)
    tabs = (f'<div class="tabs"><a href="/q/{e(row.id)}" class="{"on" if view != "ops" else ""}">답</a>'
            f'<a href="/q/{e(row.id)}?view=ops" class="{"on" if view == "ops" else ""}">운영자 보기</a></div>')
    meta = (f'<p class="small muted">{when(row.ts)} · {e(SOURCE_KO.get(row.source, row.source))} · '
            f'기준 main@{e((row.commit or "")[:7])} · 고정 링크 <code>/q/{e(row.id)}</code></p>')
    head = f"<h1>{e(row.question)}</h1>{meta}<div>{chips(row, domain_names)}</div>{tabs}"
    if view != "ops":
        if row.has_answer:
            body_html = md(parsed["body"])
        else:
            body_html = f"<p>{e(parsed['body'] or row.answer or '아직 답이 없어요.')}</p>"
        foot = ""
        if parsed["has_flow"] or parsed["has_spec"]:
            kind = "로직 흐름도" if parsed["has_flow"] else "화면 초안(와이어프레임·변경안)"
            foot += f'<p class="foot">🖼 이 답에는 {kind} 파일이 있어요. 지금은 슬랙 스레드에서 볼 수 있어요(콘솔 보관은 다음 버전).</p>'
        fups = "".join(f'<li><a href="/search?{urlencode({"q": q})}">{e(q)}</a></li>' for q in parsed["followups"])
        fup_html = f"<h2>이어서 찾아보기</h2><ul>{fups}</ul>" if fups else ""
        body = (head + (f'<div class="card">{freshness_badge(fresh, latest)}</div>' if row.has_answer else "")
                + f'<div class="card answer">{body_html}{foot}</div>'
                + ("<h2>근거</h2>" + cite_list([c for c in cites if c["verified"]]) if row.has_answer else "")
                + fup_html)
        return page(row.question[:40], body, "search")

    # 운영자 보기
    sc = row.scope or {}
    scope_html = (f'<table><tr><th>유형</th><td>{e(sc.get("kind", "-"))}</td></tr>'
                  f'<tr><th>도메인</th><td>{e(", ".join(domain_names.get(d, d) for d in row.domains) or "전체")}</td></tr>'
                  f'<tr><th>스코핑 확신도</th><td>{e(sc.get("confidence", "-"))}</td></tr>'
                  f'<tr><th>검색 힌트</th><td>{e(", ".join(sc.get("keywords") or []) or "-")}</td></tr>'
                  f'<tr><th>판단 이유</th><td>{e(sc.get("reason", "-"))}</td></tr></table>')
    read, write, fresh_in = usage.get("cache_read_tokens") or 0, usage.get("cache_write_tokens") or 0, usage.get("input_tokens") or 0
    total_in = read + write + fresh_in
    hit = f"{100 * read / total_in:.0f}%" if total_in else "-"
    usage_html = (f'<table><tr><th>경로</th><td>{e(ROUTE_KO.get(row.route or "", row.route))}</td></tr>'
                  f'<tr><th>모델</th><td>{e(usage.get("model") or "-")}</td></tr>'
                  f'<tr><th>시간</th><td>{row.duration_ms / 1000:.1f}초 · 모델 호출 {usage.get("num_turns") or 0}회</td></tr>'
                  f'<tr><th>토큰</th><td>입력 {total_in:,} (캐시 재사용 {hit}, 쓰기 {write:,}) · 출력 {usage.get("output_tokens") or 0:,}</td></tr>'
                  f'<tr><th>비용</th><td>{money(row.cost)} <span class="small muted">토큰 × 공식 단가, 스코핑 포함</span></td></tr>'
                  f'<tr><th>오류</th><td>{e(row.error or "-")}</td></tr></table>')
    step_html = ("<ol class='steps'>" + "".join(f"<li>{e(tool_step(t, a))}</li>" for t, a in steps) + "</ol>"
                 if steps else '<p class="muted">도구 호출 없음 (되묻기·거절·빠른 종료)</p>')
    fb_html = ("<ul>" + "".join(f"<li>{when(f['ts'])} {e(f['value'])} <span class='small muted'>{e(f['user_id'])}</span></li>"
                                 for f in fb_events) + "</ul>") if fb_events else '<p class="muted">없음</p>'
    reasons = "".join(f'<span class="chip bad">{e(CHECK_REASONS[r])}</span>' for r in row.check_reasons) or "없음"
    body = (head + f"<h2>확인 필요 사유</h2><p>{reasons}</p>"
            + f"<h2>스코핑</h2>{scope_html}<h2>처리</h2>{usage_html}"
            + f"<h2>근거 전체 (검증·제외)</h2>{cite_list(cites)}"
            + f"<h2>탐색 과정 ({len(steps)}단계)</h2>{step_html}"
            + f"<h2>피드백</h2>{fb_html}"
            + f'<h2>답변 원문</h2><pre class="card small" style="white-space:pre-wrap">{e(row.answer or "")}</pre>')
    return page(row.question[:40], body, "search")


def check_page(rows: list[Row], domain_names: dict[str, str], params: dict) -> str:
    counts = {k: sum(1 for r in rows if k in r.check_reasons) for k in CHECK_REASONS}
    reason = params.get("reason", "")
    shown = [r for r in rows if not reason or reason in r.check_reasons]
    filt = " ".join(
        f'<a class="chip" href="/check?{urlencode({**params, "reason": k})}">{e(v)} {counts[k]}</a>'
        for k, v in CHECK_REASONS.items() if counts[k])
    src = params.get("source", "slack")
    other = "all" if src == "slack" else "slack"
    body = ("<h1>확인 필요</h1><p class='muted'>최근 30일, 낮은 확신도·👎·확인 요청·오류·한도 도달·범위 미스가 있는 질문이에요.</p>"
            + (f"<p>{filt} <a class='small' href='/check'>전체</a></p>" if filt else "")
            + f'<p class="small"><a href="/check?{urlencode({**params, "source": other})}">'
              f'{"평가·CLI 질문도 보기" if src == "slack" else "슬랙만 보기"}</a></p>'
            + ('<div class="card">✅ 확인할 것 없음</div>' if not shown
               else row_table(shown, domain_names, show_source=src != "slack")))
    return page("확인 필요", body, "check")


def _pct(v) -> str:
    return e(v) if v not in (None, "") else "-"


def eval_page(runs: list[dict]) -> str:
    if not runs:
        return page("평가", "<h1>평가</h1><div class='card muted'>평가 결과가 없어요. <code>askcode eval</code>을 먼저 돌려 주세요.</div>", "eval")
    trs = []
    for i, r in enumerate(runs):
        s = r["summary"]
        cmp_link = (f' · <a href="/eval/compare?{urlencode({"a": runs[i + 1]["name"], "b": r["name"]})}">이전과 비교</a>'
                    if i + 1 < len(runs) else "")
        trs.append(f'<tr><td><a href="/eval/{e(r["name"])}">{e(r["name"].removeprefix("eval-"))}</a>{cmp_link}</td>'
                   f'<td>{s.get("items", "-")}</td><td><b>{_pct(s.get("correct"))}</b></td><td>{_pct(s.get("behavior"))}</td>'
                   f'<td>{_pct(s.get("evidence_recall"))}</td><td>{money(s.get("total_cost_usd") or 0)}</td>'
                   f'<td>{s.get("avg_duration_s", "-")}초</td></tr>')
    body = ("<h1>평가 이력</h1><p class='muted'>골든 세트(<code>eval/golden.yaml</code>) 회차별 결과. 문항 수가 다른 회차는 일부 문항만 돌린 것이에요.</p>"
            '<div class="table-scroll"><table><tr><th>회차</th><th>문항</th><th>정답률</th><th>행동 일치</th><th>근거 재현율</th>'
            f'<th>비용</th><th>평균 시간</th></tr>{"".join(trs)}</table></div>')
    return page("평가", body, "eval")


def eval_run_page(run: dict) -> str:
    trs = "".join(
        f'<tr><td>{e(r["id"])}</td><td>{e(r.get("type", ""))}</td><td>{e(r.get("question", ""))}</td>'
        f'<td>{"✅" if r.get("correct") else ("❔" if r.get("correct") is None else "❌")}</td>'
        f'<td>{e(ROUTE_KO.get(r.get("route", ""), r.get("route", "")))}</td><td>{money(r.get("cost_usd") or 0)}</td>'
        f'<td class="small muted">{e(r.get("judge_reason", ""))}</td></tr>' for r in run["results"])
    body = (f'<h1>{e(run["name"])}</h1><p><a href="/eval">← 평가 이력</a></p><div class="table-scroll"><table>'
            f"<tr><th>문항</th><th>유형</th><th>질문</th><th>정답</th><th>경로</th><th>비용</th><th>판정 이유</th></tr>{trs}</table></div>")
    return page("평가", body, "eval")


def compare_page(a: dict, b: dict, rows: list[dict]) -> str:
    def mark(v):
        return "✅" if v is True else ("❔" if v is None else "❌")

    counts = {k: sum(1 for r in rows if r["change"] == k) for k in ("새로 맞음", "새로 틀림", "그대로", "새 문항")}
    summary = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
    trs = "".join(
        f'<tr><td>{e(r["id"])}</td><td><span class="chip {"ok" if r["change"] == "새로 맞음" else "bad" if r["change"] == "새로 틀림" else ""}">'
        f'{e(r["change"])}</span></td><td>{mark(r["before"])} → {mark(r["after"])}</td><td>{e(r["question"])}</td>'
        f'<td class="small muted">{e(r["reason"])}</td></tr>' for r in rows)
    body = (f'<h1>평가 비교</h1><p class="muted">{e(a["name"])} → {e(b["name"])} · {e(summary)}</p>'
            f'<p><a href="/eval">← 평가 이력</a></p><div class="table-scroll"><table>'
            f"<tr><th>문항</th><th>변화</th><th>정답</th><th>질문</th><th>이후 판정 이유</th></tr>{trs}</table></div>")
    return page("평가 비교", body, "eval")


def not_found(what: str) -> str:
    return page("없음", f"<h1>{e(what)}</h1><p><a href='/'>홈으로</a></p>")

