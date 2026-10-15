"""슬랙 봇 (Socket Mode). 외부로 열린 포트 없이 슬랙에 바깥 방향으로만 연결한다."""

from __future__ import annotations

import asyncio
import logging
import re

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from askcode.pipeline.answer import AnswerService
from askcode.slack.blocks import answer_blocks, citation_line, fallback_text, thinking_blocks
from askcode.store.db import Store

log = logging.getLogger(__name__)
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def build_app(bot_token: str, service: AnswerService, store: Store, escalation_channel: str | None) -> AsyncApp:
    app = AsyncApp(token=bot_token)
    inflight = asyncio.Semaphore(4)  # 동시 에이전트 실행 수 제한

    async def handle_question(client, channel: str, user: str, text: str, thread_ts: str) -> None:
        question = MENTION_RE.sub("", text).strip()
        if not question:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                          text="무엇이 궁금한지 함께 적어 주세요. 예: `의안 상세 화면은 네이티브야 웹뷰야?`")
            return
        placeholder = await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                                    text="확인 중…", blocks=thinking_blocks())
        async with inflight:
            answer = await service.answer(question, source="slack", user_id=user, channel=channel, thread_ts=thread_ts)
        await client.chat_update(channel=channel, ts=placeholder["ts"], text=fallback_text(answer),
                                 blocks=answer_blocks(answer))

    @app.event("app_mention")
    async def on_mention(event, client):
        await handle_question(client, event["channel"], event.get("user", ""), event.get("text", ""),
                              event.get("thread_ts") or event["ts"])

    @app.event("message")
    async def on_dm(event, client):
        # DM만 처리. 봇 메시지·수정·삭제 이벤트는 무시한다(봇 자신의 답변을 다시 처리하지 않도록).
        if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
            return
        await handle_question(client, event["channel"], event.get("user", ""), event.get("text", ""),
                              event.get("thread_ts") or event["ts"])

    async def _thread(body) -> tuple[str, str]:
        msg = body["message"]
        return body["channel"]["id"], msg.get("thread_ts") or msg["ts"]

    @app.action("show_evidence")
    async def on_show_evidence(ack, body, client):
        await ack()
        query_id = body["actions"][0]["value"]
        rows = store.execute(
            "SELECT path, start_line, end_line, verified, reason FROM citations WHERE query_id=? ORDER BY id",
            (query_id,),
        ).fetchall()
        commit = store.execute("SELECT commit_sha FROM queries WHERE id=?", (query_id,)).fetchone()
        base = service.repo.web_base
        lines = []
        for r in rows:
            loc = f"{r['path']}:{r['start_line']}" + (f"-{r['end_line']}" if r["end_line"] != r["start_line"] else "")
            url = f"{base}/blob/{commit['commit_sha'] if commit else 'main'}/{r['path']}#L{r['start_line']}"
            mark = "✅" if r["verified"] else f"⚠️ {r['reason']}"
            lines.append(f"{mark} <{url}|`{loc}`>")
        tools = store.execute("SELECT tool, input_json FROM tool_calls WHERE query_id=? ORDER BY id", (query_id,)).fetchall()
        trail = "\n".join(f"{i + 1}. `{t['tool']}` {t['input_json'][:120]}" for i, t in enumerate(tools))
        channel, thread_ts = await _thread(body)
        text = "*인용 전체*\n" + ("\n".join(lines) or "(없음)") + "\n\n*탐색 경로*\n" + (trail or "(없음)")
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text[:3900])

    async def _feedback(ack, body, client, value: str, note: str):
        await ack()
        query_id = body["actions"][0]["value"]
        store.log_feedback(query_id, body["user"]["id"], value)
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
        a = store.execute("SELECT answer, confidence FROM query_results WHERE query_id=?", (query_id,)).fetchone()
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


__all__ = ["build_app", "serve", "citation_line"]
