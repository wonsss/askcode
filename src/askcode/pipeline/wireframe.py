"""와이어프레임 결과물 처리: 답변에서 HTML을 꺼내고, 슬랙에 파일로 올려도 안전하게 정제한다.

생성된 HTML은 기획자가 브라우저로 연다. 모델 출력은 신뢰하지 않으므로(코드·문서 속 지시문이 섞일 수 있다)
허용 목록에 있는 태그·속성만 남기고, 스크립트·외부 리소스·이벤트 속성을 모두 제거한 뒤
외부 접근을 막는 CSP를 넣는다.
"""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser

HTML_BLOCK_RE = re.compile(r"```html\s*\n(.*?)```", re.S | re.I)
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


def wireframe_filename(question: str, query_id: str) -> str:
    slug = re.sub(r"[^\w가-힣]+", "-", question).strip("-")[:30] or "wireframe"
    return f"wireframe-{slug}-{query_id[:6]}.html"
