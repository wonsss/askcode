"""와이어프레임 결과물 처리: 모델이 낸 화면 명세(JSON)를 결정적으로 HTML로 그리고, 슬랙에 올려도 안전하게 만든다.

모델은 HTML·CSS 대신 짧은 화면 명세만 출력한다(출력 토큰 절감). 렌더러가 회색조 저충실도 HTML을 만들고
모든 텍스트를 이스케이프하며 외부 접근을 막는 CSP를 넣는다.
명세 대신 자유 HTML이 들어오면 허용 목록 정제(sanitize_html)로 처리한다. 모델 출력은 신뢰하지 않는다
(코드·문서 속 지시문이 섞일 수 있다).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from html.parser import HTMLParser

HTML_BLOCK_RE = re.compile(r"```html\s*\n(.*?)```", re.S | re.I)
SPEC_BLOCK_RE = re.compile(r"```(?:json|wireframe)\s*\n(.*?)```", re.S | re.I)
MAX_HTML_BYTES = 200_000

ALLOWED_TAGS = {
    "html", "head", "body", "title", "style", "meta",
    "div", "span", "p", "br", "hr", "strong", "b", "em", "i", "small", "s", "u", "mark", "sup", "sub",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "header", "footer", "main", "nav", "section", "article", "aside", "figure", "figcaption",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "button", "input", "label", "select", "option", "textarea", "fieldset", "legend",
    "a", "img", "details", "summary", "blockquote", "code", "pre",
}
VOID_TAGS = {"br", "hr", "meta", "input", "img", "col"}
# 내용까지 통째로 버리는 태그
DROP_WITH_CONTENT = {"script", "iframe", "object", "embed", "noscript", "template", "svg", "math", "frame", "frameset"}
ALLOWED_ATTRS = {
    "class", "id", "style", "title", "role", "alt", "for", "name", "type", "placeholder", "value",
    "disabled", "checked", "selected", "readonly", "rows", "cols", "colspan", "rowspan", "lang", "dir",
    "width", "height", "open", "charset", "href",
}
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
       "form-action 'none'; base-uri 'none'")
CSS_BLOCKED_RE = re.compile(r"@import[^;]*;?|url\s*\([^)]*\)|expression\s*\(|behavior\s*:|-moz-binding", re.I)


def extract_html(text: str) -> tuple[str | None, str]:
    """답변에서 첫 ```html 블록을 꺼낸다. (html 또는 None, html 블록을 뺀 나머지 텍스트)"""
    m = HTML_BLOCK_RE.search(text or "")
    if not m:
        return None, text
    rest = (text[: m.start()] + text[m.end():]).strip()
    return m.group(1).strip(), re.sub(r"\n{3,}", "\n\n", rest)


def _clean_css(css: str) -> str:
    return CSS_BLOCKED_RE.sub("", css)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.drop_depth = 0
        self.in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_WITH_CONTENT:
            self.drop_depth += 1
            return
        if self.drop_depth or tag not in ALLOWED_TAGS:
            return
        if tag == "meta" and not any(k == "charset" for k, _ in attrs):
            return  # http-equiv(refresh 등)·기타 meta는 버린다. CSP는 따로 넣는다
        kept = []
        for k, v in attrs:
            k = k.lower()
            if k not in ALLOWED_ATTRS and not k.startswith("aria-"):
                continue  # on* 이벤트, src, srcset, action, formaction 등은 여기서 빠진다
            v = v or ""
            if k == "href" and not v.startswith("#"):
                continue  # 외부·javascript: 링크 금지, 페이지 안 이동만 허용
            if k == "style":
                v = _clean_css(v)
            if k == "type" and tag == "button":
                v = "button"
            kept.append(f' {k}="{html_lib.escape(v, quote=True)}"' if v or k not in {"disabled", "checked", "selected", "readonly", "open"}
                        else f" {k}")
        if tag == "img":
            kept = [a for a in kept if not a.startswith(" src")]
        self.out.append(f"<{tag}{''.join(kept)}>")
        if tag == "style":
            self.in_style = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag == "style":
            self.in_style = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        if self.in_style:
            self.out.append(_clean_css(data).replace("</", "<\\/"))
        else:
            self.out.append(html_lib.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.drop_depth:
            self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.drop_depth:
            self.out.append(f"&#{name};")

    # 주석·선언·처리 명령(<!-- -->, <!DOCTYPE>, <?xml?>)은 버린다. DOCTYPE은 다시 붙인다.
    def handle_comment(self, data: str) -> None: ...
    def handle_decl(self, decl: str) -> None: ...
    def handle_pi(self, data: str) -> None: ...


def sanitize_html(raw: str) -> str:
    """허용 목록 정제 + CSP. 결과는 완결된 HTML 문서다."""
    raw = raw[:MAX_HTML_BYTES]
    p = _Sanitizer()
    p.feed(raw)
    p.close()
    body = "".join(p.out)
    head_inject = (f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{CSP}">'
                   '<meta name="viewport" content="width=device-width, initial-scale=1">')
    if re.search(r"<head[^>]*>", body):
        body = re.sub(r"<head[^>]*>", lambda m: m.group(0) + head_inject, body, count=1)
    else:
        body = f"<head>{head_inject}</head>" + body
    if not body.lstrip().startswith("<html"):
        body = f"<html lang=\"ko\">{body}</html>"
    return "<!DOCTYPE html>\n" + body


# ── 화면 명세 → HTML ─────────────────────────────────────────────
# 명세 스키마 (모델이 ```json 블록으로 낸다. 모든 필드는 선택이고, 모르는 키는 무시한다)
#   {"title": str, "platform": "app"|"web",
#    "screens": [{"name": str, "variant": "current"|"proposed"(선택), "elements": [요소, ...]}]}
#   variant: proposed는 변경안 프레임(스크린샷 경로는 이것만 그리고 원본 스크린샷과 비교), current는 재구성한 현재 화면.
# 요소 공통: "type"(아래 중 하나), "note"(있으면 번호 주석 ①②…을 달고 프레임 아래 설명에 쓴다),
#   "new"(bool, 변경안 프레임에서만: 새로 추가·바뀐 요소 → 점선 테두리 + 🆕)
#   header      title, left, right(문자열), large(bool: 큰 제목형, 기본은 내비게이션 바형)
#   search      placeholder
#   tabs        items[str], active(int)            상단 탭
#   chips       items[str], active(int)            필터·정렬 칩
#   card_list   card{tag,title,desc,meta,thumb}, count(1~6)   같은 모양 카드 반복
#   list        items[str | {title, meta, indent(bool)}]       행 목록
#   section     title                               구획 제목
#   text        text, size("title"|"body"|"meta")
#   form_field  label, placeholder, tall(bool), select(bool)
#   button      label, primary(bool), full(bool)
#   box         label, height(px)                   이미지·차트 등 자리 박스
#   notice      text                                안내 문구 상자
#   table       columns[str], rows(1~8)             웹 표
#   columns     cols[[요소, ...], ...], widths[int] 웹 좌우 배치(한 단계만)
#   fab         label                               앱: 오른쪽 아래 떠 있는 버튼
#   input_bar   placeholder, button                 앱: 하단 고정 입력창
#   bottom_tabs items[str], active(int)             앱: 하단 탭 바
SPEC_TYPES = {"header", "search", "tabs", "chips", "card_list", "list", "section", "text", "form_field", "button",
              "box", "notice", "table", "columns", "fab", "input_bar", "bottom_tabs"}
MAX_SCREENS = 6
MAX_ELEMENTS = 40
MAX_TEXT = 200
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

SPEC_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#f3f3f3;color:#111;font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;font-size:14px}
h1{font-size:18px;margin:0 0 16px}
.row{display:flex;flex-wrap:wrap;gap:32px;align-items:flex-start}
.col{width:390px}.col.web{width:1280px}
.name{font-weight:700;margin-bottom:8px}
.frame{position:relative;width:390px;height:844px;background:#fff;border:2px solid #111;border-radius:28px;overflow:hidden}
.web .frame{width:1280px;height:auto;min-height:720px;border-radius:8px;padding-bottom:24px}
.status{height:44px;font-size:11px;color:#999;padding:14px 20px 0}
.browser{height:36px;background:#f3f3f3;border-bottom:1px solid #ddd;padding:9px 16px;font-size:11px;color:#999}
.el{position:relative;margin:0 20px 12px}
.nav{display:flex;justify-content:space-between;align-items:center;margin:0 0 12px;padding:6px 20px 10px;color:#555;border-bottom:1px solid #ddd}
.nav b{color:#111}
.large{font-size:24px;font-weight:700;display:flex;justify-content:space-between;align-items:center}
.large span{font-size:14px;font-weight:400;color:#555}
.search{border:1px solid #ddd;background:#f3f3f3;border-radius:10px;padding:11px 12px;color:#999}
.tabs{display:flex;border-bottom:1px solid #ddd;margin:0 0 12px;padding:0 20px}
.tabs span{flex:1;text-align:center;padding:10px 0;color:#999}
.tabs .on{color:#111;font-weight:700;border-bottom:2px solid #111}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{border:1px solid #ddd;border-radius:16px;padding:6px 12px;color:#555;font-size:13px}
.chip.on{background:#111;color:#fff;border-color:#111}
.card{padding:14px;border:1px solid #ddd;border-radius:12px;margin-bottom:10px;display:flex;gap:12px}
.card>div{flex:1}.card b{display:block;margin-bottom:4px}
.thumb{width:64px;height:64px;background:#ddd;border-radius:8px;color:#555;font-size:11px;display:flex;align-items:center;justify-content:center;text-align:center}
.tag{display:inline-block;background:#f3f3f3;color:#555;font-size:11px;padding:2px 8px;border-radius:8px;margin-bottom:6px}
.desc{color:#555}.meta{color:#999;font-size:12px;margin-top:4px}
.li{padding:10px 0;border-bottom:1px solid #f3f3f3}.li.indent{margin-left:20px;padding-left:10px;border-left:2px solid #ddd}
.section{font-weight:700;border-top:8px solid #f3f3f3;padding-top:10px;margin-left:0;margin-right:0;padding-left:20px;padding-right:20px}
.t-title{font-size:20px;font-weight:700}.t-body{line-height:1.6}.t-meta{color:#999;font-size:12px}
.field label{display:block;font-size:12px;color:#555;margin-bottom:6px}
.input{border:1px solid #ddd;border-radius:10px;padding:12px;color:#999}.input.tall{height:200px}
.btn{display:inline-block;border:1px solid #111;border-radius:12px;padding:11px 16px;font-weight:700;text-align:center}
.btn.primary{background:#111;color:#fff}.btn.full{display:block}
.box{background:#ddd;color:#555;font-size:12px;border-radius:8px;display:flex;align-items:center;justify-content:center;text-align:center}
.notice{padding:12px;background:#f3f3f3;border-radius:10px;color:#555;font-size:12px;line-height:1.5}
table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #ddd;padding:10px;text-align:left}th{color:#555;font-size:12px}td{color:#999}
.cols{display:flex;gap:24px;margin:0 20px 12px}.cols>div>.el{margin-left:0;margin-right:0}
.fab{position:absolute;right:24px;bottom:96px;background:#111;color:#fff;border-radius:24px;padding:13px 18px;font-weight:700}
.inputbar{position:absolute;left:0;right:0;bottom:0;padding:10px 16px 28px;border-top:1px solid #ddd;background:#fff;display:flex;gap:8px}
.inputbar .input{flex:1;border-radius:18px;padding:10px 14px;background:#f3f3f3}
.tabbar{position:absolute;left:16px;right:16px;bottom:20px;height:56px;border:1px solid #ddd;border-radius:28px;background:#fff;display:flex;justify-content:space-around;align-items:center;color:#999;font-size:12px}
.tabbar .on{color:#111;font-weight:700}
.web .fab,.web .inputbar,.web .tabbar{position:static;margin:12px 20px}
.num{display:inline-block;min-width:18px;height:18px;line-height:18px;padding:0 4px;text-align:center;border-radius:9px;background:#111;color:#fff;font-size:11px;font-weight:700}
.nav,.tabs{position:relative}
.mark{position:absolute;right:-14px;top:-6px;z-index:2}
.tabs .num,.tabbar .num{flex:none;padding:0 4px;color:#fff;border:0}
.nav .mark,.tabs .mark,.section .mark,.inputbar .mark,.tabbar .mark{right:6px;top:-9px}
.legend{margin-top:12px;font-size:12px;color:#555;line-height:1.7}.legend .num{margin-right:6px}
.sub{margin:-8px 0 16px;color:#555;font-size:12px}
.variant{display:inline-block;border:1px solid #111;border-radius:8px;padding:1px 8px;margin-right:6px;font-size:12px}
.variant.proposed{background:#111;color:#fff}
.isnew{outline:2px dashed #111;outline-offset:3px}
.isnew:not(.fab):not(.tabbar):not(.inputbar){position:relative}
.newtag{position:absolute;left:-10px;top:-12px;z-index:3;font-size:13px;line-height:1}
.nav>.newtag,.tabs>.newtag,.tabbar>.newtag,.inputbar>.newtag,.section>.newtag{left:4px;top:2px}
"""


class WireframeSpecError(ValueError):
    """명세를 읽을 수 없음 (JSON 오류, 화면 없음 등)."""


def extract_spec(text: str) -> tuple[str | None, str]:
    """답변에서 첫 ```json 블록을 꺼낸다. (명세 원문 또는 None, 블록을 뺀 나머지 텍스트)"""
    m = SPEC_BLOCK_RE.search(text or "")
    if not m:
        return None, text
    rest = (text[: m.start()] + text[m.end():]).strip()
    return m.group(1).strip(), re.sub(r"\n{3,}", "\n\n", rest)


def parse_spec(raw: str) -> dict:
    """명세 JSON을 읽어 최소 구조를 확인한다. 실패하면 WireframeSpecError."""
    raw = raw[:MAX_HTML_BYTES]
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        spec = _salvage_spec(raw)  # 요소 하나의 오타로 전체를 버리지 않는다
        if not spec["screens"]:
            raise WireframeSpecError(f"JSON 오류: {e}") from e
    if not isinstance(spec, dict):
        raise WireframeSpecError("명세가 객체가 아님")
    screens = [s for s in spec.get("screens") or [] if isinstance(s, dict) and isinstance(s.get("elements"), list)]
    if not screens or not any(_valid_elements(s["elements"]) for s in screens):
        raise WireframeSpecError("그릴 화면이 없음")
    spec["screens"] = screens[:MAX_SCREENS]
    return spec


ELEMENT_START_RE = re.compile(r'\{\s*"type"\s*:')
SCREEN_NAME_RE = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _object_end(text: str, start: int) -> int:
    """text[start]의 '{'와 짝이 맞는 '}' 다음 위치. 문자열 안의 괄호는 무시한다. 잘려서 안 닫히면 -1."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            esc, in_str = (False, in_str) if esc else (c == "\\", c != '"')
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _salvage_spec(raw: str) -> dict:
    """깨진 명세에서 요소 객체를 하나씩 읽어 살린다. 읽히지 않는 요소만 버린다."""
    def field(key: str, text: str) -> str | None:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        return json.loads(f'"{m.group(1)}"') if m else None

    screens: list[dict] = []
    pos = 0
    while True:
        m = ELEMENT_START_RE.search(raw, pos)
        if not m:
            break
        # 이 요소 앞에 새 화면 이름이 있으면 화면을 연다 (요소 객체 안쪽은 이미 건너뛰었다)
        for nm in SCREEN_NAME_RE.finditer(raw, pos, m.start()):
            head = raw[raw.rfind("{", 0, nm.start()): m.start()]
            screens.append({"name": json.loads(f'"{nm.group(1)}"'), "variant": field("variant", head), "elements": []})
        end = _object_end(raw, m.start())
        if end < 0:
            break  # 잘린 요소부터는 버린다
        try:
            el = json.loads(raw[m.start():end])
        except json.JSONDecodeError:
            el = None
        if isinstance(el, dict):
            if not screens:
                screens.append({"name": "", "elements": []})
            screens[-1]["elements"].append(el)
        pos = end
    for sc in screens:
        if not sc.get("variant"):
            sc.pop("variant", None)
    head = raw[: (ELEMENT_START_RE.search(raw) or re.search(r"$", raw)).start()]
    return {"title": field("title", head), "platform": field("platform", head),
            "screens": [sc for sc in screens if _valid_elements(sc["elements"])]}


def is_comparison(spec: dict) -> bool:
    """'현재'와 '변경안' 프레임이 모두 있는 비교 목업인가."""
    variants = {s.get("variant") for s in spec.get("screens") or []}
    return {"current", "proposed"} <= variants


def has_proposal(spec: dict) -> bool:
    """변경안 프레임이 있는가. 스크린샷 변경 요청은 변경안만 그리고 원본 스크린샷과 비교한다."""
    return any(s.get("variant") == "proposed" for s in spec.get("screens") or [])


def new_element_count(spec: dict) -> int:
    """변경안 프레임에서 🆕로 표시될 요소 수 (columns 안쪽 포함)."""
    def count(elements: list) -> int:
        n = 0
        for e in _valid_elements(elements):
            n += bool(e.get("new") or e.get("changed"))
            if e["type"] == "columns":
                n += sum(count(c) for c in _items(e.get("cols")) if isinstance(c, list))
        return n
    return sum(count(s["elements"]) for s in spec.get("screens") or [] if s.get("variant") == "proposed")


def _valid_elements(elements: list) -> list[dict]:
    return [e for e in elements[:MAX_ELEMENTS] if isinstance(e, dict) and e.get("type") in SPEC_TYPES]


def _t(v: object) -> str:
    """모델이 준 값 → 이스케이프한 짧은 텍스트. true/false는 글자로 찍지 않는다(예: "thumb": true → 빈 자리 박스)."""
    if v is None or isinstance(v, (bool, dict, list)):
        return ""
    return html_lib.escape(str(v)[:MAX_TEXT], quote=True)


def _items(v: object) -> list:
    return v[:12] if isinstance(v, list) else []


def _int(v: object, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _active(e: dict) -> int:
    """active는 번호(0부터) 또는 항목 이름 둘 다 받는다."""
    items, v = _items(e.get("items")), e.get("active")
    if isinstance(v, str) and v in items:
        return items.index(v)
    return _int(v, 0, 0, 99)


def _num(n: int) -> str:
    return CIRCLED[n - 1] if n <= len(CIRCLED) else str(n)


class _Renderer:
    def __init__(self, web: bool) -> None:
        self.web = web
        self.notes: list[str] = []
        self.proposed = False  # 변경안 프레임을 그리는 중이면 new 표시를 켠다

    def mark(self, e: dict) -> str:
        note = _t(e.get("note"))
        if not note:
            return ""
        self.notes.append(note)
        return f'<span class="num mark">{_num(len(self.notes))}</span>'

    def elements(self, elements: list, depth: int = 0) -> str:
        return "".join(self.element(e, depth) for e in _valid_elements(elements))

    def element(self, e: dict, depth: int) -> str:
        if self.proposed and (e.get("new") or e.get("changed")):
            out = self._element(e, depth, '<span class="newtag">🆕</span>')
            return out.replace('class="', 'class="isnew ', 1)
        return self._element(e, depth, "")

    def _element(self, e: dict, depth: int, tag: str) -> str:
        k, m = e["type"], tag + self.mark(e)
        active = _active(e)
        if k == "header":
            if e.get("large"):
                return f'<div class="el large">{_t(e.get("title"))}<span>{_t(e.get("right"))}</span>{m}</div>'
            return (f'<div class="nav"><span>{_t(e.get("left"))}</span><b>{_t(e.get("title"))}</b>'
                    f'<span>{_t(e.get("right"))}</span>{m}</div>')
        if k == "search":
            return f'<div class="el"><div class="search">{_t(e.get("placeholder")) or "검색"}</div>{m}</div>'
        if k == "tabs":
            tabs = "".join((f'<span class="on">{_t(x)}</span>' if i == active else f"<span>{_t(x)}</span>")
                           for i, x in enumerate(_items(e.get("items"))))
            return f'<div class="tabs">{tabs}{m}</div>'
        if k == "chips":
            chips = "".join(f'<span class="chip{" on" if i == active else ""}">{_t(x)}</span>'
                            for i, x in enumerate(_items(e.get("items"))))
            return f'<div class="el chips">{chips}{m}</div>'
        if k == "card_list":
            c = e.get("card") if isinstance(e.get("card"), dict) else {}
            thumb = f'<div class="thumb">{_t(c.get("thumb"))}</div>' if c.get("thumb") else ""
            card = (f'<div class="card">{thumb}<div>'
                    + (f'<span class="tag">{_t(c.get("tag"))}</span>' if c.get("tag") else "")
                    + f'<b>{_t(c.get("title")) or "제목"}</b>'
                    + (f'<div class="desc">{_t(c.get("desc"))}</div>' if c.get("desc") else "")
                    + (f'<div class="meta">{_t(c.get("meta"))}</div>' if c.get("meta") else "")
                    + "</div></div>")
            return f'<div class="el">{card * _int(e.get("count"), 3, 1, 6)}{m}</div>'
        if k == "list":
            rows = []
            for x in _items(e.get("items")):
                x = x if isinstance(x, dict) else {"title": x}
                meta = f'<div class="meta">{_t(x.get("meta"))}</div>' if x.get("meta") else ""
                rows.append(f'<div class="li{" indent" if x.get("indent") else ""}">{_t(x.get("title"))}{meta}</div>')
            return f'<div class="el">{"".join(rows)}{m}</div>'
        if k == "section":
            return f'<div class="el section">{_t(e.get("title"))}{m}</div>'
        if k == "text":
            size = e.get("size") if e.get("size") in ("title", "body", "meta") else "body"
            return f'<div class="el t-{size}">{_t(e.get("text"))}{m}</div>'
        if k == "form_field":
            ph = _t(e.get("placeholder")) + (" ▾" if e.get("select") else "")
            return (f'<div class="el field"><label>{_t(e.get("label"))}</label>'
                    f'<div class="input{" tall" if e.get("tall") else ""}">{ph}</div>{m}</div>')
        if k == "button":
            cls = "btn" + (" primary" if e.get("primary") else "") + (" full" if e.get("full") else "")
            return f'<div class="el"><span class="{cls}">{_t(e.get("label")) or "버튼"}</span>{m}</div>'
        if k == "box":
            h = _int(e.get("height"), 120, 24, 600)
            return f'<div class="el"><div class="box" style="height:{h}px">{_t(e.get("label"))}</div>{m}</div>'
        if k == "notice":
            return f'<div class="el notice">{_t(e.get("text"))}{m}</div>'
        if k == "table":
            cols = _items(e.get("columns"))
            head = "".join(f"<th>{_t(c)}</th>" for c in cols)
            row = "<tr>" + "<td>—</td>" * len(cols) + "</tr>"
            return (f'<div class="el"><table><thead><tr>{head}</tr></thead>'
                    f'<tbody>{row * _int(e.get("rows"), 3, 1, 8)}</tbody></table>{m}</div>')
        if k == "columns":
            if depth:
                return ""
            cols = [c for c in _items(e.get("cols")) if isinstance(c, list)][:4]
            widths = [_int(w, 1, 1, 12) for w in _items(e.get("widths"))]
            parts = "".join(f'<div style="flex:{widths[i] if i < len(widths) else 1}">{self.elements(c, depth + 1)}</div>'
                            for i, c in enumerate(cols))
            return f'<div class="cols">{parts}</div>{m}'
        if k == "fab":
            return f'<div class="fab">{_t(e.get("label")) or "+"}{m}</div>'
        if k == "input_bar":
            return (f'<div class="inputbar"><div class="input">{_t(e.get("placeholder"))}</div>'
                    f'<span class="btn primary">{_t(e.get("button")) or "등록"}</span>{m}</div>')
        if k == "bottom_tabs":
            tabs = "".join((f'<span class="on">{_t(x)}</span>' if i == active else f"<span>{_t(x)}</span>")
                           for i, x in enumerate(_items(e.get("items"))))
            return f'<div class="tabbar">{tabs}{m}</div>'
        return ""

    def screen(self, s: dict) -> str:
        self.notes = []
        variant = s.get("variant") if s.get("variant") in ("current", "proposed") else ""
        self.proposed = variant == "proposed"
        body = self.elements(s["elements"])
        top = '<div class="browser">브라우저</div>' if self.web else '<div class="status">9:41</div>'
        legend = "".join(f'<div><span class="num">{_num(i)}</span>{n}</div>' for i, n in enumerate(self.notes, 1))
        badge = (f'<span class="variant {variant}">{"현재" if variant == "current" else "변경안"}</span>'
                 if variant else "")
        return (f'<div class="col{" web" if self.web else ""}"><div class="name">{badge}{_t(s.get("name"))}</div>'
                f'<div class="frame">{top}{body}</div><div class="legend">{legend}</div></div>')


def render_spec(spec: dict) -> str:
    """명세 → 완결된 HTML 문서. 모든 텍스트를 이스케이프하고 CSP를 넣는다."""
    r = _Renderer(web=spec.get("platform") == "web")
    title = _t(spec.get("title")) or "와이어프레임"
    screens = "".join(r.screen(s) for s in spec["screens"])
    if is_comparison(spec):
        sub = ('<p class="sub">현재 화면은 스크린샷과 코드로 재구성한 저충실도 그림이라 실제와 세부가 다를 수 있어요. '
               '변경안에서 점선 테두리와 🆕는 새로 추가·바뀐 요소예요.</p>')
    elif has_proposal(spec):
        sub = ('<p class="sub">원본 스크린샷과 나란히 놓고 비교해 보세요. 저충실도 그림이라 실제 디자인과 세부가 다를 수 있어요. '
               '점선 테두리와 🆕는 새로 추가·바뀐 요소예요.</p>')
    else:
        sub = ""
    return ("<!DOCTYPE html>\n<html lang=\"ko\"><head>"
            f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{CSP}">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title}</title><style>{SPEC_CSS}</style></head>"
            f'<body><h1>{title}</h1>{sub}<div class="row">{screens}</div></body></html>')


def wireframe_filename(question: str, query_id: str, prefix: str = "wireframe") -> str:
    slug = re.sub(r"[^\w가-힣]+", "-", question).strip("-")[:30] or prefix
    return f"{prefix}-{slug}-{query_id[:6]}.html"
