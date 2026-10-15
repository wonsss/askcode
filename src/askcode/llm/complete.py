"""단순 LLM 호출(분류·요약·스코핑용). 호출을 이 함수 하나로 모아 모델 교체·비용 기록을 한곳에서 처리한다.

프로바이더를 바꿔야 하면(예: 규제 환경의 클라우드 내 모델) 이 함수 내부만 교체한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic

from askcode.settings import Settings

Tier = Literal["small", "large"]


@dataclass
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class LLM:
    def __init__(self, settings: Settings):
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        self._client = AsyncAnthropic(api_key=key)
        self._models = {"small": settings.model_small, "large": settings.model_large}

    async def complete(
        self, prompt: str, tier: Tier = "small", cache_prefix: str | None = None, max_tokens: int = 1024
    ) -> Completion:
        """cache_prefix는 질문마다 반복되는 고정 맥락(도메인 맵 등)으로, 프롬프트 캐시 대상이다."""
        system = (
            [{"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}}] if cache_prefix else []
        )
        model = self._models[tier]
        msg = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        u = msg.usage
        return Completion(
            text=text,
            model=model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
