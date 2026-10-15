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


def _thinking(tier: Tier) -> dict:
    """소형 모델 호출은 분류·추출이라 thinking을 끈다.

    claude-haiku-5-5는 thinking이 기본으로 켜져 있어, 짧은 max_tokens를 thinking이 다 써 버리면
    JSON이 잘리거나 비어 나온다(실측: 스코핑 400토큰 전부 thinking). disabled는 effort high 이하에서만
    허용되며 기본 effort(medium)라 문제없다. 대형 모델(sonnet-5-5)은 disabled가 400이라 건드리지 않는다.
    """
    return {"thinking": {"type": "disabled"}} if tier == "small" else {}


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
            **_thinking(tier),
        )
        return self._completion(msg, model)

    async def vision(self, prompt: str, image: bytes, media_type: str, tier: Tier = "small",
                     max_tokens: int = 800) -> Completion:
        """스크린샷에서 화면 문구를 읽을 때 쓴다."""
        import base64

        model = self._models[tier]
        msg = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(image).decode()}},
                {"type": "text", "text": prompt},
            ]}],
            **_thinking(tier),
        )
        return self._completion(msg, model)

    @staticmethod
    def _completion(msg, model: str) -> Completion:
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
