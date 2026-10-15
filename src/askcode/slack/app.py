"""슬랙 봇 (Socket Mode). 외부로 열린 포트 없이 슬랙에 바깥 방향으로만 연결한다."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from askcode.llm.complete import LLM
from askcode.pipeline.answer import AnswerService, evidence_notes
from askcode.pipeline.screenshot import ALLOWED_TYPES, MAX_BYTES, extract_screen, screenshot_context
from askcode.slack.blocks import answer_blocks, evidence_blocks, fallback_text, thinking_blocks
from askcode.store.db import Store

log = logging.getLogger(__name__)
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
WRAP_CHARS = "`\"'“”‘’"


def clean_question(text: str) -> str:
    """멘션을 지우고, 안내 예시를 복사해 붙인 백틱·따옴표를 걷어낸다."""
    q = MENTION_RE.sub("", text).strip()
    while len(q) >= 2 and q[0] in WRAP_CHARS and q[-1] in WRAP_CHARS:
        q = q[1:-1].strip()
    return q
CLARIFY_RE = re.compile(r"^clarify_pick_")
DEFAULT_SCREEN_QUESTION = "이 화면은 어떤 코드로 구현돼 있고, 주요 버튼은 어떤 조건으로 동작해?"


def build_app(bot_token: str, service: AnswerService, store: Store, escalation_channel: str | None,
              llm: LLM | None = None) -> AsyncApp:
    app = AsyncApp(token=bot_token)
    inflight = asyncio.Semaphore(4)  # 동시 에이전트 실행 수 제한

    async def screenshot_extra(files: list[dict]) -> str:
        img = next((f for f in files or [] if f.get("mimetype") in ALLOWED_TYPES and f.get("size", 0) <= MAX_BYTES), None)
        if not img or not llm:
            return ""
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(img["url_private_download"], headers={"Authorization": f"Bearer {bot_token}"})
            r.raise_for_status()
        texts, desc, _ = await extract_screen(llm, r.content, img["mimetype"])
        return screenshot_context(texts, desc) if texts else ""

    async def handle_question(client, channel: str, user: str, text: str, thread_ts: str, files: list[dict]) -> None:
        question = clean_question(text)
        has_image = any(f.get("mimetype") in ALLOWED_TYPES for f in files or [])
        if not question and not has_image:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                          text="무엇이 궁금한지 함께 적어 주세요. 예: `의안 상세 화면은 네이티브야 웹뷰야?`")
            return
        placeholder = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="확인 중…",
            blocks=thinking_blocks("⏳ 화면을 읽고 코드를 찾고 있어요…" if has_image else "⏳ 코드를 확인하고 있어요…"))
        try:
            extra = await screenshot_extra(files) if has_image else ""
            async with inflight:
                answer = await service.answer(question or DEFAULT_SCREEN_QUESTION, source="slack", user_id=user,
                                              channel=channel, thread_ts=thread_ts, extra_context=extra)
            await client.chat_update(channel=channel, ts=placeholder["ts"], text=fallback_text(answer),
                                     blocks=answer_blocks(answer))
        except Exception:
            log.exception("question failed")
            await client.chat_update(channel=channel, ts=placeholder["ts"],
                                     text="처리 중 오류가 났어요. 잠시 후 다시 시도해 주세요.", blocks=[])

    @app.event("app_mention")
    async def on_mention(event, client):
        await handle_question(client, event["channel"], event.get("user", ""), event.get("text", ""),
                              event.get("thread_ts") or event["ts"], event.get("files", []))

    @app.event("message")
    async def on_dm(event, client):
        # DM만 처리. 봇 메시지·수정·삭제 이벤트는 무시한다(봇 자신의 답변을 다시 처리하지 않도록).
        if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        await handle_question(client, event["channel"], event.get("user", ""), event.get("text", ""),
                              event.get("thread_ts") or event["ts"], event.get("files", []))

    @app.action(CLARIFY_RE)
    async def on_clarify(ack, body, client):
        await ack()
        query_id, _, domain = body["actions"][0]["value"].partition("|")
        q = store.execute("SELECT question, user_id, channel, thread_ts FROM queries WHERE id=?", (query_id,)).fetchone()
        if not q:
            return
        channel, ts = body["channel"]["id"], body["message"]["ts"]
        label = body["actions"][0]["text"]["text"]
        await client.chat_update(channel=channel, ts=ts, text="확인 중…",
                                 blocks=thinking_blocks(f"⏳ '{label}' 범위에서 찾고 있어요…"))
        async with inflight:
            answer = await service.answer(q["question"], source="slack", user_id=body["user"]["id"], channel=q["channel"],
                                          thread_ts=q["thread_ts"], forced_domains=[] if domain == "*" else [domain])
        await client.chat_update(channel=channel, ts=ts, text=fallback_text(answer), blocks=answer_blocks(answer))

    async def _thread(body) -> tuple[str, str]:
        msg = body["message"]
        return body["channel"]["id"], msg.get("thread_ts") or msg["ts"]

    @app.action("show_evidence")
    async def on_show_evidence(ack, body, client):
        await ack()
        query_id = body["actions"][0]["value"]
        rows = store.execute("SELECT source, path, start_line, end_line, verified, reason FROM citations "
                             "WHERE query_id=? ORDER BY id", (query_id,)).fetchall()
        commit = store.execute("SELECT commit_sha FROM queries WHERE id=?", (query_id,)).fetchone()
        res = store.execute("SELECT route, cost_usd, duration_ms, num_turns, input_tokens, output_tokens, cache_read_tokens, "
                            "cache_write_tokens, answer FROM query_results WHERE query_id=?", (query_id,)).fetchone()
        notes = evidence_notes(res["answer"]) if res else {}
        base = service.repo.web_base
        cites = []
        for r in rows:
            doc = store.doc_file(r["path"]) if r["source"] in ("notion", "slack") else None
            sha = commit["commit_sha"] if commit else "main"
            url = doc["url"] if doc else f"{base}/blob/{sha}/{r['path']}#L{r['start_line']}"
            cites.append({"source": r["source"], "path": r["path"], "start": r["start_line"], "end": r["end_line"],
                          "verified": bool(r["verified"]), "reason": r["reason"], "url": url,
                          "note": notes.get((r["path"], r["start_line"]), "")})
        tools = store.execute("SELECT tool, input_json FROM tool_calls WHERE query_id=? ORDER BY id", (query_id,)).fetchall()
        steps = []
        for t in tools:
            try:
                steps.append((t["tool"], json.loads(t["input_json"])))
            except ValueError:
                steps.append((t["tool"], {}))
        channel, thread_ts = await _thread(body)
        blocks = evidence_blocks(cites, steps, dict(res) if res else None)
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="근거 전체", blocks=blocks)

    async def _feedback(ack, body, client, value: str, note: str):
        await ack()
        store.log_feedback(body["actions"][0]["value"], body["user"]["id"], value)
        await client.chat_postEphemeral(channel=body["channel"]["id"], user=body["user"]["id"], text=note)

    @app.action("feedback_up")
    async def on_up(ack, body, client):
        await _feedback(ack, body, client, "up", "피드백 고마워요 👍")

    @app.action("feedback_down")
    async def on_down(ack, body, client):
        await _feedback(ack, body, client, "down", "피드백이 기록됐어요. 틀린 부분을 스레드에 적어 주시면 개선에 쓰입니다.")

    @app.action("escalate")
    async def on_escalate(ack, body, client):
        query_id = body["actions"][0]["value"]
        await _feedback(ack, body, client, "escalate", "개발 채널에 확인 요청을 올렸어요." if escalation_channel
                        else "확인 요청 채널이 설정되지 않았어요(ASKCODE_ESCALATION_CHANNEL).")
        if not escalation_channel:
            return
        q = store.execute("SELECT question, channel, thread_ts FROM queries WHERE id=?", (query_id,)).fetchone()
        a = store.execute("SELECT confidence FROM query_results WHERE query_id=?", (query_id,)).fetchone()
        permalink = await client.chat_getPermalink(channel=q["channel"], message_ts=q["thread_ts"])
        await client.chat_postMessage(
            channel=escalation_channel,
            text=(f"<@{body['user']['id']}>님의 확인 요청\n*질문* {q['question']}\n"
                  f"*봇 확신도* {a['confidence'] if a else '-'}\n<{permalink['permalink']}|원래 스레드 보기>"),
        )

    return app


async def serve(app: AsyncApp, app_token: str) -> None:
    handler = AsyncSocketModeHandler(app, app_token)
    log.info("AskCode Slack bot started (Socket Mode)")
    await handler.start_async()
