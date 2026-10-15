"""Claude Agent SDK 기반 AgentRunner.

- 내장 도구(Bash, Read, Edit 등)는 모두 끄고(tools=[]) 우리 읽기 전용 도구만 허용한다.
- 허용 목록 밖 도구 호출은 PreToolUse 훅에서 한 번 더 차단한다(이중 안전장치).
- 설정 파일·CLAUDE.md 자동 로딩을 막기 위해 setting_sources=[]와 빈 작업 디렉터리를 쓴다.
"""

from __future__ import annotations

import logging
import tempfile
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from askcode.agent.base import AgentRequest, AgentResponse, ToolSpec

log = logging.getLogger(__name__)

SERVER_NAME = "askcode"


def _sdk_tool(spec: ToolSpec):
    @tool(spec.name, spec.description, spec.input_schema)
    async def _run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            text = await spec.handler(args)
        except Exception as exc:  # 도구 오류는 모델에게 돌려주고 루프는 계속
            log.exception("tool %s failed", spec.name)
            text = f"도구 오류: {exc}"
        return {"content": [{"type": "text", "text": text}]}

    return _run


class ClaudeAgentRunner:
    def __init__(self, model: str, api_key: str | None):
        self.model = model
        self.api_key = api_key
        self._workdir = tempfile.mkdtemp(prefix="askcode-agent-")

    async def run(self, request: AgentRequest) -> AgentResponse:
        allowed = [f"mcp__{SERVER_NAME}__{t.name}" for t in request.tools]
        server = create_sdk_mcp_server(SERVER_NAME, tools=[_sdk_tool(t) for t in request.tools])

        async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            if input_data.get("tool_name") in allowed:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "AskCode는 읽기 전용 도구만 사용할 수 있습니다.",
                }
            }

        options = ClaudeAgentOptions(
            tools=[],
            allowed_tools=allowed,
            mcp_servers={SERVER_NAME: server},
            system_prompt=request.system_prompt,
            model=request.model or self.model,
            max_turns=request.max_turns,
            max_budget_usd=request.max_budget_usd,
            permission_mode="dontAsk",
            setting_sources=[],
            cwd=self._workdir,
            env={"ANTHROPIC_API_KEY": self.api_key} if self.api_key else {},
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]},
        )

        started = time.monotonic()
        texts: list[str] = []
        response = AgentResponse(text="", model=request.model or self.model)
        try:
            async for message in query(prompt=request.question, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            texts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            response.tool_calls.append(block.name)
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    response.input_tokens = usage.get("input_tokens", 0) or 0
                    response.output_tokens = usage.get("output_tokens", 0) or 0
                    response.cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
                    response.cache_write_tokens = usage.get("cache_creation_input_tokens", 0) or 0
                    response.cost_usd = message.total_cost_usd or 0.0
                    response.num_turns = message.num_turns
                    if message.result:
                        texts = [message.result]
                    if message.is_error:
                        response.error = message.subtype
        except Exception as exc:
            log.exception("agent run failed")
            response.error = f"{type(exc).__name__}: {exc}"
        response.duration_ms = int((time.monotonic() - started) * 1000)
        response.text = (texts[-1] if texts else "").strip()
        return response
