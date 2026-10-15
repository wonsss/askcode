"""슬랙 채널 수집기. 관리자가 지정한 공개 채널의 최근 N일 스레드만 읽는다.

봇 메시지·봇 자신의 답변은 제외해, 봇이 자기 답을 근거로 다시 인용하는 순환을 막는다.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from slack_sdk import WebClient

log = logging.getLogger(__name__)


def _ts_to_iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


class SlackSource:
    def __init__(self, bot_token: str, channels: list[str], days: int = 90, client: WebClient | None = None):
        self.client = client or WebClient(token=bot_token)
        self.channels = channels
        self.days = days
        self._names: dict[str, str] = {}

    def _user(self, uid: str | None) -> str:
        if not uid:
            return "알 수 없음"
        if uid not in self._names:
            try:
                u = self.client.users_info(user=uid)["user"]
                self._names[uid] = u.get("profile", {}).get("display_name") or u.get("real_name") or uid
            except Exception:
                self._names[uid] = uid
        return self._names[uid]

    @staticmethod
    def _human(m: dict) -> bool:
        return not m.get("bot_id") and m.get("subtype") in (None, "thread_broadcast") and bool(m.get("text"))

    def collect(self) -> list[dict]:
        base = self.client.auth_test().get("url", "").rstrip("/")
        oldest = str(time.time() - self.days * 86400)
        items: list[dict] = []
        for ch in self.channels:
            info = self.client.conversations_info(channel=ch)["channel"]
            if info.get("is_private"):
                log.warning("skip private channel %s", ch)  # 비공개 채널은 소스로 쓰지 않는다
                continue
            name = info.get("name", ch)
            cursor = None
            while True:
                resp = self.client.conversations_history(channel=ch, oldest=oldest, limit=200, cursor=cursor)
                for m in resp["messages"]:
                    if not self._human(m):
                        continue
                    thread = [m]
                    if m.get("reply_count"):
                        replies = self.client.conversations_replies(channel=ch, ts=m["ts"], limit=200)["messages"][1:]
                        thread += [r for r in replies if self._human(r)]
                    lines = [f"# #{name} 스레드 ({_ts_to_iso(m['ts'])[:10]})", ""]
                    for t in thread:
                        lines.append(f"**{self._user(t.get('user'))}** ({_ts_to_iso(t['ts'])[:16].replace('T', ' ')} UTC)")
                        lines.extend(t["text"].splitlines())
                        lines.append("")
                    first = m["text"].splitlines()[0][:60]
                    items.append({
                        "path": f"slack/{name}/{m['ts'].replace('.', '')}.md",
                        "title": f"#{name}: {first}",
                        "url": f"{base}/archives/{ch}/p{m['ts'].replace('.', '')}",
                        "updated_at": _ts_to_iso(thread[-1]["ts"]),
                        "markdown": "\n".join(lines),
                    })
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        log.info("slack: %d threads", len(items))
        return items
