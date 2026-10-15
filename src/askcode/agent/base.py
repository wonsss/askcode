"""에이전트 포트. 파이프라인은 이 계약만 알고, 프로바이더별 루프는 구현체가 맡는다."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """프로바이더 중립 도구 정의. handler는 문자열 결과를 돌려준다."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class AgentRequest:
    question: str
    system_prompt: str
    tools: list[ToolSpec]
    max_turns: int = 12
    max_budget_usd: float | None = None
    model: str | None = None
    effort: str | None = None  # low | medium | high | xhigh | max. None이면 모델 기본값


@dataclass
class AgentResponse:
    text: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    error: str | None = None
    tool_calls: list[str] = field(default_factory=list)


class AgentRunner(Protocol):
    async def run(self, request: AgentRequest) -> AgentResponse: ...
