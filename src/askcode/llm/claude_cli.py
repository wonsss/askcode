"""`claude` CLI를 하위 프로세스로 띄워 돌리는 Completer — 구독 로그인 모드(로컬 PoC 전용).

SlackBotManager와 같은 방식이다: `claude -p`를 띄우고 프롬프트는 stdin으로 넣고,
`--output-format json`(텍스트) / `stream-json`(이미지)의 result 이벤트에서 답과 usage를 읽는다.

- `--safe-mode`: 훅·스킬·MCP·CLAUDE.md 자동 로딩을 끈다. 구독 로그인·모델 선택·권한은 유지된다.
- `--tools ""`: 내장 도구를 끈다. 분류·추출 호출에는 도구가 필요 없다.
- `--bare`는 절대 쓰지 않는다 — OAuth·키체인 읽기까지 끊겨 구독 로그인이 안 된다.
- 부모가 물려준 ANTHROPIC_API_KEY·ANTHROPIC_AUTH_TOKEN·ANTHROPIC_BASE_URL은 빈 값으로 덮는다.
  값이 남아 있으면 구독이 아니라 그 키로 API 과금이 나간다(빈 문자열은 CLI가 '없음'으로 본다).
- max_tokens 인자는 CLI에 대응 플래그가 없어 받기만 하고 무시한다.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from askcode.llm.complete import Completion, Tier
from askcode.settings import Settings

AUTH_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

# cache_prefix가 없는 호출에 쓰는 최소 시스템 프롬프트. CLI 기본 프롬프트(Claude Code용, ~2.8k토큰)를
# 매 호출마다 태우지 않게 한다.
DEFAULT_SYSTEM = "You are part of a Slack-answer pipeline. Follow the user's instruction exactly and output only what is asked."

TIMEOUT_SEC = 150  # API 직접 호출(60s)보다 여유 — CLI 기동·세션 오버헤드 포함

_AUTH_MARKERS = ("not logged in", "authenticate", "oauth", "invalid api key", "401")


class CliError(Exception):
    """claude CLI 호출 실패. fatal=True면 재시도해도 안 되는 오류(로그인 없음·바이너리 없음 등)."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def _fatal(message: str) -> bool:
    m = message.lower()
    return any(k in m for k in _AUTH_MARKERS)


def find_claude() -> str | None:
    """claude 실행 파일 경로. launchd의 PATH는 ~/.local/bin 같은 사용자 설치 경로를 모른다."""
    candidates = (
        os.environ.get("ASKCODE_CLAUDE_PATH"),
        shutil.which("claude"),
        str(Path.home() / ".local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        str(Path.home() / ".claude/local/claude"),
    )
    return next((c for c in candidates if c and os.access(c, os.X_OK)), None)


def cli_env() -> dict[str, str]:
    """하위 claude 프로세스용 환경. 상속된 API 과금 변수를 비워 구독 로그인이 쓰이게 한다."""
    env = dict(os.environ)
    for k in AUTH_ENV_KEYS:
        env[k] = ""
    env["PATH"] = (env.get("PATH") or "/usr/bin:/bin") + f":{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin"
    # claude의 자격 증명 조회는 USER를 쓴다 — 메뉴 막대 앱·launchd의 최소 환경에는 없을 수 있어 채운다
    if not env.get("USER"):
        env["USER"] = env.get("LOGNAME") or getpass.getuser()
    env.setdefault("LOGNAME", env["USER"])
    return env


def auth_status() -> dict | None:
    """`claude auth status` 결과(JSON). 바이너리가 없거나 읽기 실패면 None. 호출 비용 0."""
    exe = find_claude()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True,
                           timeout=15, env=cli_env())
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def cli_preflight() -> str | None:
    """구독 모드 시작 점검: 바이너리 → 로그인 상태 → 실제 1회 호출. 문제가 있으면 원인 문장, 없으면 None."""
    exe = find_claude()
    if not exe:
        return "claude CLI를 찾을 수 없어요(구독 모드). Claude Code를 설치하세요."
    st = auth_status()
    if st is None:
        return "`claude auth status`를 읽지 못했어요."
    if not st.get("loggedIn"):
        return "claude CLI가 로그아웃 상태예요. 구독 계정으로 `claude auth login` 하세요."
    try:
        r = subprocess.run(
            [exe, "-p", "Respond with exactly OK.", "--output-format", "text", "--safe-mode", "--tools", ""],
            capture_output=True, text=True, timeout=60, env=cli_env(), cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        return "claude CLI 응답이 60초를 넘겼어요."
    if r.returncode != 0 or "OK" not in r.stdout:
        detail = (r.stderr.strip() or r.stdout.strip())[-200:]
        return f"claude CLI 호출 실패: {detail or '빈 응답'}"
    return None


async def _run(args: list[str], stdin_data: bytes, cwd: Path) -> str:
    """stdout 전체를 돌려준다. 종료 코드가 0이 아니거나 시간 초과면 CliError."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=cli_env(), cwd=str(cwd))
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_data), TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CliError(f"claude CLI가 {TIMEOUT_SEC}초 안에 끝나지 않았어요") from None
    if proc.returncode != 0:
        detail = (err.decode(errors="replace").strip() or out.decode(errors="replace").strip())[-300:]
        raise CliError(f"claude CLI 종료 코드 {proc.returncode}: {detail}", fatal=_fatal(detail))
    return out.decode(errors="replace")


def _result(obj: dict, model: str) -> Completion:
    """result 이벤트 → Completion. 비용은 기존처럼 토큰×단가로 따로 계산하므로 usage만 챙긴다."""
    if obj.get("is_error"):
        msg = str(obj.get("result") or obj.get("subtype") or "unknown")
        raise CliError(f"claude CLI 실패: {msg}", fatal=_fatal(msg))
    u = obj.get("usage") or {}
    return Completion(
        text=str(obj.get("result") or "").strip(),
        model=model,
        input_tokens=u.get("input_tokens", 0) or 0,
        output_tokens=u.get("output_tokens", 0) or 0,
        cache_read_tokens=u.get("cache_read_input_tokens", 0) or 0,
        cache_write_tokens=u.get("cache_creation_input_tokens", 0) or 0,
    )


class CliLLM:
    """구독 로그인 Completer. LLM과 같은 계약(complete/vision)을 `claude -p`로 구현한다."""

    def __init__(self, settings: Settings):
        self._models = {"small": settings.model_small, "large": settings.model_large}
        # cwd를 빈 디렉터리로 두어 저장소 파일·프로젝트 설정이 새지 않게 한다
        self._workdir = Path(tempfile.mkdtemp(prefix="askcode-cli-"))

    def _exe(self) -> str:
        exe = find_claude()
        if not exe:
            raise CliError("claude CLI를 찾을 수 없어요(구독 모드). Claude Code를 설치하세요.", fatal=True)
        return exe

    async def complete(
        self, prompt: str, tier: Tier = "small", cache_prefix: str | None = None, max_tokens: int = 1024
    ) -> Completion:
        model = self._models[tier]
        args = [self._exe(), "-p", "--output-format", "json", "--safe-mode", "--tools", "",
                "--model", model, "--system-prompt", cache_prefix or DEFAULT_SYSTEM]
        out = await _run(args, prompt.encode(), self._workdir)
        try:
            return _result(json.loads(out), model)
        except json.JSONDecodeError:
            raise CliError(f"claude CLI 출력이 JSON이 아니에요: {out[:200]}") from None

    async def vision(self, prompt: str, image: bytes, media_type: str, tier: Tier = "small",
                     max_tokens: int = 800) -> Completion:
        model = self._models[tier]
        payload = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": prompt}]}})
        # 이미지 메시지는 stream-json 입력으로만 보낼 수 있고, 그때 출력도 stream-json이어야 한다
        args = [self._exe(), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--safe-mode", "--tools", "", "--model", model]
        out = await _run(args, (payload + "\n").encode(), self._workdir)
        for line in reversed(out.splitlines()):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                return _result(obj, model)
        raise CliError(f"claude CLI 출력에 result가 없어요: {out[:200]}")
