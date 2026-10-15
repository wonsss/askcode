"""Claude Agent SDK 기반 AgentRunner.

- 내장 도구(Bash, Read, Edit 등)는 모두 끄고(tools=[]) 우리 읽기 전용 도구만 허용한다.
- 허용 목록 밖 도구 호출은 PreToolUse 훅에서 한 번 더 차단한다(이중 안전장치).
- 설정 파일·CLAUDE.md 자동 로딩을 막기 위해 setting_sources=[]와 빈 작업 디렉터리를 쓴다.
"""

from __future__ import annotations

import getpass
import logging
import os
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
from askcode.llm.pricing import estimate_cost

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
    def __init__(self, model: str, api_key: str | None, base_url: str | None = None,
                 subscription: bool = False):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.subscription = subscription
        self._workdir = tempfile.mkdtemp(prefix="askcode-agent-")

    def _env(self) -> dict[str, str]:
        """하위 프로세스는 부모 환경변수를 물려받는다. 셸의 Claude 인증·주소 대신 봇 설정을 쓰도록 덮어쓴다."""
        if self.subscription:
            # 구독 모드: 하위 claude CLI가 로컬 구독 로그인을 쓰게 한다. 상속된 API 과금 변수는 비운다.
            env = {k: "" for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
            # 자격 증명 조회는 USER를 쓰는데 launchd의 최소 환경에는 없을 수 있다
            env["USER"] = os.environ.get("USER") or getpass.getuser()
            env["LOGNAME"] = os.environ.get("LOGNAME") or env["USER"]
            return env
        env: dict[str, str] = {}
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
            env["ANTHROPIC_AUTH_TOKEN"] = ""  # 셸의 Bearer 토큰이 API 키보다 우선하지 않게 비운다
        if self.base_url:
            env["ANTHROPIC_BASE_URL"] = self.base_url
        return env

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
            # 구독 모드는 토큰 과금이 없고 CLI가 리스트 단가로 계산해 예산을 일찍 친다 — 최대 대화 수로만 제한
            max_budget_usd=None if self.subscription else request.max_budget_usd,
            effort=request.effort,
            permission_mode="dontAsk",
            setting_sources=[],
            cwd=self._workdir,
            env=self._env(),
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
                    est = estimate_cost(response.model, response.input_tokens, response.output_tokens,
                                        response.cache_read_tokens, response.cache_write_tokens)
                    response.cost_usd = est if est is not None else (message.total_cost_usd or 0.0)
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
