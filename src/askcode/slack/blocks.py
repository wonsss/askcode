"""슬랙 Block Kit 메시지 구성."""

from __future__ import annotations

import re

from askcode.pipeline.answer import Answer, Citation

CONFIDENCE_LABEL = {"high": "🟢 확신도 높음", "medium": "🟡 확신도 보통", "low": "🔴 확신도 낮음 · 확인 필요"}
SOURCE_LABEL = {"code": "코드", "doc": "문서", "notion": "노션", "slack": "슬랙"}
MAX_SECTION = 2900
TOP_CITATIONS = 3


DEV_NOTE_PREFIX = "🔧"


def plain_body(text: str) -> str:
    """기획자용 본문에서 코드 서식을 걷어낸다. '🔧 개발자 참고' 줄만 코드 서식을 남긴다."""
    return "\n".join(line if line.lstrip().startswith(DEV_NOTE_PREFIX) else line.replace("`", "")
                     for line in text.splitlines())


FOOTNOTE_PREFIXES = (DEV_NOTE_PREFIX, "확인 필요", "참고:")


def split_body(text: str) -> tuple[str, list[str]]:
    """본문과 꼬리말(🔧 개발자 참고·확인 필요·참고)을 나눈다. 꼬리말은 작은 글씨 블록으로 보낸다."""
    main, foot = [], []
    for line in text.splitlines():
        (foot if line.lstrip().startswith(FOOTNOTE_PREFIXES) else main).append(line)
    return "\n".join(main).strip(), [f.strip() for f in foot if f.strip()]


def to_mrkdwn(text: str) -> str:
    """마크다운 일부를 슬랙 mrkdwn으로 바꾼다."""
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    return text


def short_path(path: str) -> str:
    """긴 경로를 '상위폴더/파일명'으로 줄인다. 전체 경로는 링크와 '근거 전체 보기'에 있다."""
    parts = path.split("/")
    return "/".join(parts[-2:])


def citation_line(c: Citation) -> str:
    loc = f"{short_path(c.path)}:{c.start}" + (f"-{c.end}" if c.end != c.start else "")
    link = f"<{c.url}|{loc}>" if c.url else loc
    note = c.note.replace("`", "").strip(" —-")
    return f"• {SOURCE_LABEL.get(c.source, '')} {link}" + (f"  {note}" if note else "")


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
    meta = [CONFIDENCE_LABEL.get(a.confidence, a.confidence)]
    if a.domain_names:
        meta.append(" · ".join(a.domain_names))
    meta.append(f"코드 기준 main@{a.commit[:7]}")
    blocks: list[dict] = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "   |   ".join(meta)}]},
    ]
    main, foot = split_body(a.body)
    # 문단마다 section을 나눠 슬랙이 긴 section을 접지 않게 한다
    for para in [p for p in main.split("\n\n") if p.strip()] or ["(빈 답변)"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": to_mrkdwn(plain_body(para))[:MAX_SECTION]}})
    if foot:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": to_mrkdwn(plain_body(f))[:MAX_SECTION]}
                                                       for f in foot[:5]]})
    if a.citations:
        lines = "\n".join(citation_line(c) for c in a.citations[:TOP_CITATIONS])
        more = f"\n외 {len(a.citations) - TOP_CITATIONS}건은 '근거 전체 보기'에서 볼 수 있어요." if len(a.citations) > TOP_CITATIONS else ""
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"*📎 근거*\n{lines}{more}"[:MAX_SECTION]}]})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "검증된 근거가 없어요. 개발자 확인을 권장합니다."}]})
    notes = []
    if a.config_dependent:
        notes.append("⚙️ 일부 값은 운영 설정에 따라 달라질 수 있어요.")
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


TOOL_LABEL = {
    "code_search": "코드 검색", "read_code": "코드 읽기", "list_dir": "폴더 보기", "find_screen": "화면 찾기",
    "find_api": "API 찾기", "find_bridge": "웹뷰↔앱 연결 찾기", "search_docs": "문서 검색", "read_doc": "문서 읽기",
    "widen_scope": "범위 넓히기",
}
ROUTE_LABEL = {"simple": "빠른 답(소형 모델)", "agent": "심층 탐색(대형 모델)"}


def tool_step(tool: str, args: dict) -> str:
    """도구 호출을 사람이 읽는 한 줄로."""
    label = TOOL_LABEL.get(tool.removeprefix("mcp__askcode__"), tool)
    parts = []
    if args.get("query"):
        parts.append(f"“{str(args['query'])[:60]}”")
    if args.get("path"):
        rng = f" {args['start']}–{args['end']}줄" if args.get("start") and args.get("end") else ""
        parts.append(short_path(str(args["path"])) + rng)
    if args.get("path_glob"):
        parts.append(f"({str(args['path_glob']).rstrip('/*')} 안에서)")
    if args.get("reason"):
        parts.append(str(args["reason"])[:60])
    return f"{label}  {' '.join(parts)}".rstrip()


def _k(n: int | None) -> str:
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def evidence_blocks(citations: list[dict], steps: list[tuple[str, dict]], result: dict | None) -> list[dict]:
    """'근거 전체 보기' 스레드 메시지. citations: source·path·start·end·verified·reason·note·url"""
    ok = [c for c in citations if c["verified"]]
    bad = [c for c in citations if not c["verified"]]

    def line(c: dict) -> str:
        loc = f"{short_path(c['path'])}:{c['start']}" + (f"-{c['end']}" if c["end"] != c["start"] else "")
        link = f"<{c['url']}|{loc}>" if c.get("url") else loc
        note = (c.get("note") or "").replace("`", "")
        tail = f"  {note}" if c["verified"] else f"  _제외: {c['reason']}_"
        return f"{'✅' if c['verified'] else '⚠️'} {SOURCE_LABEL.get(c['source'], '')} {link}{tail if (note or not c['verified']) else ''}"

    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": (
        f"*📎 근거 전체*  (검증 {len(ok)}건" + (f" · 제외 {len(bad)}건)" if bad else ")") + "\n"
        + ("\n".join(line(c) for c in ok + bad) or "(없음)"))[:MAX_SECTION]}}]
    if steps:
        trail = "\n".join(f"{i + 1}. {tool_step(t, a)}" for i, (t, a) in enumerate(steps[:25]))
        more = f"\n…외 {len(steps) - 25}단계" if len(steps) > 25 else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*🔎 탐색 과정*  ({len(steps)}단계)\n{trail}{more}"[:MAX_SECTION]}})
    if result:
        read, write, fresh = result.get("cache_read_tokens") or 0, result.get("cache_write_tokens") or 0, result.get("input_tokens") or 0
        total_in = read + write + fresh
        hit = f" · 캐시 재사용 {100 * read / total_in:.0f}%" if total_in else ""
        stat = (f"{ROUTE_LABEL.get(result.get('route'), result.get('route') or '-')} · {(result.get('duration_ms') or 0) / 1000:.1f}초"
                f" · 비용 약 ${result.get('cost_usd') or 0:.4f}\n"
                f"토큰: 입력 {_k(total_in)}{hit} · 출력 {_k(result.get('output_tokens'))} · 모델 호출 {result.get('num_turns') or 0}회")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": stat}]})
    return blocks


def fallback_text(a: Answer) -> str:
    return a.body[:300]
