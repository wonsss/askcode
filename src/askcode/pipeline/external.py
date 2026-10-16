"""외부 조사 넘겨주기. AskCode는 웹을 찾지 않는 대신, 사용자가 다른 AI에 붙여넣을 프롬프트를 만들어 준다.

프롬프트는 law-watch(비공개) 밖으로 나가는 글이다. 그래서
- 모델(스코퍼)은 조사 목적·질문만 일반어로 내고, 서비스 소개는 사람이 쓴 `public_summary`를 쓴다.
- 결정적 필터가 내부 식별자처럼 보이는 항목(경로·파일명·코드 식별자·구조 인덱스의 API·화면·브리지 이름)을 뺀다.
추가 모델 호출은 없다(스코핑 출력에 같이 나온다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_TOPICS = 5
MAX_ITEM_CHARS = 200

# 코드에서 온 것처럼 보이는 모양. 한국어 문장에는 거의 나오지 않으므로 넓게 잡는다.
LEAK_PATTERNS = [
    re.compile(r"https?://|www\.", re.I),
    re.compile(r"(?<![\w가-힣])/[a-z][\w\-/:\[\].]*"),             # /api/v1/..., /people/:slug
    re.compile(r"\b[a-z0-9_.\-]+/[a-z0-9_.\-\[\]()]+"),             # apps/api, src/bills (소문자 경로)
    re.compile(r"\b\w+\.(?:tsx?|jsx?|py|md|json|ya?ml|sql|swift|kt|java|go|rb|sh|env|lock)\b", re.I),
    re.compile(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b"),                  # Controller.method, obj.field
    re.compile(r"\b[a-z]{2,}[A-Z][A-Za-z0-9]*\b"),                  # camelCase (iOS·iPhone은 통과)
    re.compile(r"\b\w+_\w+\b"),                                     # snake_case, ENV_NAME
    re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b"),                    # kebab-case (manifest·브리지 이름)
    re.compile(r"`"),
]


@dataclass
class ExternalPrompt:
    text: str
    kept: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


# 구조 인덱스에 흔히 나오지만 조사 질문에 써도 되는 일반 단어
GENERIC_WORDS = {"admin", "mobile", "native", "embedded", "external", "webview", "users", "items", "detail",
                 "search", "status", "count", "public", "metadata"}


def internal_terms(structure_rows: list[tuple[str, str | None]], extra: tuple[str, ...] = ()) -> set[str]:
    """구조 인덱스(key, label)와 설정의 금지어에서 내부 이름을 모은다. 5자 미만·일반 단어는 뺀다(오탐 방지)."""
    terms: set[str] = set()
    for key, label in structure_rows:
        for raw in (key, label or ""):
            for part in re.split(r"[\s.:/]+", raw):
                if len(part) >= 5 and part.lower() not in GENERIC_WORDS and not part.isdigit():
                    terms.add(part.lower())
    terms.update(t.lower() for t in extra if t)
    return terms


def leaks(text: str, terms: set[str]) -> bool:
    """내부 식별자 모양이거나 내부 이름(영문은 단어 단위, 한글은 부분 일치)이 들어 있으면 True."""
    if any(p.search(text) for p in LEAK_PATTERNS):
        return True
    low = text.lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    return any((t in words) if t.isascii() else (t in low) for t in terms)


def build_external_prompt(external: dict | None, public_summary: str, terms: set[str]) -> ExternalPrompt | None:
    """스코퍼의 {"goal", "topics"}로 붙여넣기용 프롬프트를 만든다. 남는 조사 질문이 없으면 None."""
    if not external:
        return None
    goal = str(external.get("goal") or "").strip()[:MAX_ITEM_CHARS]
    topics = [str(t).strip()[:MAX_ITEM_CHARS] for t in (external.get("topics") or []) if str(t).strip()]
    kept: list[str] = []
    dropped: list[str] = []
    for t in dict.fromkeys(topics):
        (dropped if leaks(t, terms) else kept).append(t)
    kept = kept[:MAX_TOPICS]
    if goal and leaks(goal, terms):
        dropped.insert(0, goal)
        goal = ""
    if not kept:
        return ExternalPrompt("", [], dropped) if dropped else None
    intro = f"{public_summary.strip() or '저희 서비스'}에서 새 기능을 기획하고 있습니다."
    lines = [intro, ""]
    if goal:
        lines += [f"목표: {goal}", ""]
    lines.append("조사해 주세요:")
    lines += [f"{i}. {t}" for i, t in enumerate(kept, 1)]
    lines += [
        "",
        "조건:",
        "- 확인 가능한 출처 링크를 달아 주세요.",
        "- 여러 방법을 비교할 때는 표로 정리하고 비용·이용 약관·제한 사항을 함께 적어 주세요.",
        "- 확실하지 않은 내용은 추정이라고 표시해 주세요.",
    ]
    return ExternalPrompt("\n".join(lines), kept, dropped)


EXTERNAL_ONLY_TEXT = ("이 질문은 외부 조사가 필요해요. AskCode는 사내 코드와 기획 문서만 읽고 웹은 찾지 않아요. "
                      "아래 프롬프트를 다른 AI에 붙여넣어 조사해 보세요.")
EXTERNAL_ONLY_NO_PROMPT = "이 질문은 외부 조사가 필요해요. AskCode는 사내 코드와 기획 문서만 읽고 웹은 찾지 않아요."
EXTERNAL_NOTE = ("AskCode는 웹을 찾지 않아요. 아래 프롬프트를 다른 AI에 붙여넣어 보세요. 내부 코드 정보는 넣지 않았지만, "
                 "붙여넣기 전에 내용과 사내 외부 AI 사용 정책을 확인해 주세요.")
EXTERNAL_DROPPED_ONLY = "외부 조사가 필요한 부분이 있지만, 내부 정보가 섞일 수 있어 조사용 프롬프트를 만들지 않았어요."


# 외부 조사 프롬프트가 따로 붙을 때 본문에서 지우는 메타 문장 ("외부 리서치는 다루지 않았어요" 류).
# "외부 신호 수집"·"외부 링크"·"외부 영상" 같은 코드 개념은 건드리지 않도록 좁게 잡는다.
META_RE = re.compile(r"외부\s*(?:리서치|조사)|외부\s*(?:자료|웹)[^.]{0,20}?(?:조회|검색|찾지|확인하지|못)")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?]\*\*)\s+")  # "…해요.** 다음 문장"도 나눈다
CHECK_PREFIX = "확인 필요"


def _strip_check_line(line: str) -> str:
    """'확인 필요: A, B, 외부 리서치 항목(…)은 다루지 않았어요.'에서 외부 조사 항목만 뺀다. 남는 게 없으면 ''."""
    head, _, rest = line.partition(":")
    parts, depth, cur = [], 0, ""
    for ch in rest:  # 괄호 안 쉼표로는 나누지 않는다
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    kept = [p.strip() for p in parts if p.strip() and not META_RE.search(p)]
    if len(kept) == len([p for p in parts if p.strip()]):
        return line
    if not kept:
        return ""
    return f"{head}: {', '.join(kept).rstrip('.')}."


def strip_external_meta(body: str) -> str:
    """본문에서 외부 조사 메타 문장을 지운다. 🔧 줄은 두고, 확인 필요 줄은 항목 단위로 뺀다."""
    out = []
    for line in body.splitlines():
        s = line.lstrip()
        if not META_RE.search(line) or s.startswith("🔧"):
            out.append(line)
            continue
        if s.startswith(CHECK_PREFIX):
            line = _strip_check_line(line)
        else:
            indent = line[: len(line) - len(s)]
            kept = [x for x in SENTENCE_SPLIT_RE.split(s) if x.strip() and not META_RE.search(x)]
            line = (indent + " ".join(kept)) if kept else ""
        if line.strip():
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()  # 지운 줄 자리의 빈 줄을 합친다
