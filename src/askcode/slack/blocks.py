"""슬랙 Block Kit 메시지 구성."""

from __future__ import annotations

import re

from askcode.pipeline.answer import Answer, Citation

CONFIDENCE_LABEL = {"high": "🟢 확신도 높음", "medium": "🟡 확신도 보통", "low": "🔴 확신도 낮음 · 확인 필요"}
SOURCE_ICON = {"code": "💻", "doc": "📄", "notion": "📝", "slack": "💬"}
MAX_SECTION = 2900
TOP_CITATIONS = 6


def to_mrkdwn(text: str) -> str:
    """마크다운 일부를 슬랙 mrkdwn으로 바꾼다."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    return text


def citation_line(c: Citation) -> str:
    loc = f"{c.path}:{c.start}" + (f"-{c.end}" if c.end != c.start else "")
    note = f" — {c.note}" if c.note else ""
    link = f"<{c.url}|`{loc}`>" if c.url else f"`{loc}`"
    return f"{SOURCE_ICON.get(c.source, '•')} {link}{note}"


def thinking_blocks(text: str = "⏳ 코드를 확인하고 있어요…") -> list[dict]:
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]


def clarify_blocks(a: Answer) -> list[dict]:
    buttons = [
        {"type": "button", "text": {"type": "plain_text", "text": name[:70]}, "action_id": f"clarify_pick_{i}",
         "value": f"{a.query_id}|{did}"}
        for i, (did, name) in enumerate(a.options[:5])
    ]
    buttons.append({"type": "button", "text": {"type": "plain_text", "text": "전체에서 찾기"}, "action_id": "clarify_pick_all",
                    "value": f"{a.query_id}|*"})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"🤔 {to_mrkdwn(a.body)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": "범위를 좁히면 더 정확하고 빠르게 답할 수 있어요. 범위를 알려주시면 탐색을 시작할게요."}]},
        {"type": "actions", "elements": buttons},
    ]


def refuse_blocks(a: Answer) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"🚫 {a.body}"}}]


def answer_blocks(a: Answer) -> list[dict]:
    if a.status == "clarify":
        return clarify_blocks(a)
    if a.status == "refuse":
        return refuse_blocks(a)
    if a.status == "error":
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ {a.body}"}}]
    scope = f"  ·  범위 `{', '.join(a.scope.domains)}`" if a.scope and a.scope.domains else ""
    meta = f"{CONFIDENCE_LABEL.get(a.confidence, a.confidence)}  ·  기준 `main@{a.commit[:7]}`{scope}"
    blocks: list[dict] = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": meta}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": to_mrkdwn(a.body)[:MAX_SECTION] or "(빈 답변)"}},
    ]
    if a.citations:
        lines = "\n".join(citation_line(c) for c in a.citations[:TOP_CITATIONS])
        more = f"\n외 {len(a.citations) - TOP_CITATIONS}건" if len(a.citations) > TOP_CITATIONS else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*근거*\n{lines}{more}"[:MAX_SECTION]}})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "검증된 근거가 없어요. 개발자 확인을 권장합니다."}]})
    notes = []
    if a.config_dependent:
        notes.append("⚙ 일부 값은 환경변수·운영 설정에 따라 달라질 수 있어요.")
    if a.unverified:
        notes.append(f"검증하지 못한 인용 {len(a.unverified)}건은 제외했어요.")
    if a.scope and "범위 미스" in a.scope.reason:
        notes.append("처음 정한 범위 밖까지 찾아서 답했어요.")
    if notes:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " ".join(notes)}]})
    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "근거 전체 보기"}, "action_id": "show_evidence", "value": a.query_id},
            {"type": "button", "text": {"type": "plain_text", "text": "👍"}, "action_id": "feedback_up", "value": a.query_id},
            {"type": "button", "text": {"type": "plain_text", "text": "👎"}, "action_id": "feedback_down", "value": a.query_id},
            {"type": "button", "text": {"type": "plain_text", "text": "개발자에게 확인 요청"}, "action_id": "escalate", "value": a.query_id},
        ],
    })
    return blocks


def fallback_text(a: Answer) -> str:
    return a.body[:300]
