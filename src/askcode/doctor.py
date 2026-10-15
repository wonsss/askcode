"""환경 점검 (askcode doctor). 실행 전에 무엇이 빠졌는지 한 번에 보여준다.

기본은 오프라인 점검(도구·설정·파일)만 하고, --online이면 슬랙·Claude·GitHub·노션 연결까지 확인한다.
온라인 점검은 토큰을 쓰지 않는 호출만 쓴다(인증 확인·모델 목록·ls-remote).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from askcode.settings import Settings

OK, WARN, FAIL = "✅", "⚠️ ", "❌"


@dataclass
class Check:
    status: str
    name: str
    detail: str
    required: bool = True


def _tool(name: str, args: list[str]) -> Check:
    path = shutil.which(name)
    if not path:
        return Check(FAIL, name, "설치 필요")
    out = subprocess.run([name, *args], capture_output=True, text=True).stdout.strip().splitlines()
    return Check(OK, name, out[0] if out else path)


def _secret(value, prefix: str | None = None) -> tuple[bool, str]:
    if value is None:
        return False, "비어 있음"
    v = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    if not v:
        return False, "비어 있음"
    if prefix and not v.startswith(prefix):
        return False, f"'{prefix}'로 시작해야 함"
    return True, f"설정됨 ({v[:5]}…)"


def offline_checks(s: Settings) -> list[Check]:
    checks: list[Check] = []
    py = sys.version_info
    checks.append(Check(OK if py >= (3, 12) else FAIL, "python", f"{py.major}.{py.minor}.{py.micro} (3.12 이상 필요)"))
    checks.append(_tool("git", ["--version"]))
    checks.append(_tool("rg", ["--version"]))
    checks.append(Check(OK if Path(".env").exists() else WARN, ".env",
                        "있음" if Path(".env").exists() else "없음 — cp .env.example .env", required=False))

    ok, d = _secret(s.anthropic_api_key, "sk-ant-")
    checks.append(Check(OK if ok else FAIL, "ANTHROPIC_API_KEY", d))
    ok, d = _secret(s.slack_bot_token, "xoxb-")
    checks.append(Check(OK if ok else WARN, "ASKCODE_SLACK_BOT_TOKEN", d + ("" if ok else " (serve에 필요)"), required=False))
    ok, d = _secret(s.slack_app_token, "xapp-")
    checks.append(Check(OK if ok else WARN, "ASKCODE_SLACK_APP_TOKEN", d + ("" if ok else " (serve에 필요)"), required=False))

    if s.github_app_id:
        key = s.github_app_private_key_path
        good = bool(key and key.is_file() and s.github_app_installation_id)
        checks.append(Check(OK if good else FAIL, "GitHub App", "설정됨" if good else "키 파일 경로 또는 설치 ID 확인 필요"))
    elif s.github_token:
        checks.append(Check(OK, "GitHub", "읽기 전용 토큰 사용", required=False))
    else:
        checks.append(Check(WARN, "GitHub", "인증 없음 — 공개 레포만 가능. 비공개 레포면 ASKCODE_GITHUB_TOKEN 또는 GitHub App 필요", required=False))

    notion = bool(s.notion_token and s.notion_root_page_id)
    checks.append(Check(OK if notion else WARN, "노션 소스", "설정됨" if notion else "미설정 (선택)", required=False))
    chans = [c for c in s.slack_source_channels.split(",") if c.strip()]
    checks.append(Check(OK if chans else WARN, "슬랙 소스 채널", f"{len(chans)}개" if chans else "미설정 (선택)", required=False))

    try:
        s.data_dir.mkdir(parents=True, exist_ok=True)
        probe = s.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        checks.append(Check(OK, "데이터 폴더", str(s.data_dir.resolve())))
    except OSError as exc:
        checks.append(Check(FAIL, "데이터 폴더", f"쓰기 불가: {exc}"))
    for f in ("repos.yaml", "domains.yaml", "glossary.yaml"):
        p = s.config_dir / f
        checks.append(Check(OK if p.is_file() else FAIL, f"config/{f}", "있음" if p.is_file() else "없음"))

    if s.db_path.exists():
        from askcode.store.db import Store

        st = Store(s.db_path)
        n = st.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        commit = st.execute("SELECT commit_sha FROM repo_state LIMIT 1").fetchone()
        docs = st.execute("SELECT source, COUNT(*) n FROM doc_files GROUP BY source").fetchall()
        doc_info = ", ".join(f"{r['source']} {r['n']}" for r in docs) or "없음"
        checks.append(Check(OK if n else WARN, "인덱스", f"파일 {n}개 · 커밋 {commit['commit_sha'][:7] if commit else '-'} · 문서 {doc_info}"
                            if n else "비어 있음 — askcode sync", required=False))
    else:
        checks.append(Check(WARN, "인덱스", "아직 없음 — askcode sync", required=False))
    return checks


def online_checks(s: Settings) -> list[Check]:
    checks: list[Check] = []
    if s.anthropic_api_key:
        try:
            from anthropic import Anthropic

            models = {m.id for m in Anthropic(api_key=s.anthropic_api_key.get_secret_value()).models.list(limit=100).data}
            missing = [m for m in (s.model_small, s.model_large) if m not in models]
            checks.append(Check(WARN if missing else OK, "Claude API",
                                f"연결됨 · 모델 확인 필요: {', '.join(missing)}" if missing else "연결됨 · 설정 모델 사용 가능"))
        except Exception as exc:
            checks.append(Check(FAIL, "Claude API", f"{type(exc).__name__}: {str(exc)[:120]}"))
    if s.slack_bot_token:
        try:
            from slack_sdk import WebClient

            r = WebClient(token=s.slack_bot_token.get_secret_value()).auth_test()
            checks.append(Check(OK, "슬랙 봇 토큰", f"{r['team']} 워크스페이스 · 봇 {r['user']}"))
        except Exception as exc:
            checks.append(Check(FAIL, "슬랙 봇 토큰", str(exc)[:120]))
    from askcode.config import load_repos
    from askcode.indexing.repo_sync import GitHubAuth

    for repo in load_repos(s.config_dir):
        try:
            args = ["git", *GitHubAuth(s).git_config_args(), "ls-remote", "--heads", repo.url, repo.ref]
            out = subprocess.run(args, capture_output=True, text=True, timeout=60)
            good = out.returncode == 0 and out.stdout.strip()
            checks.append(Check(OK if good else FAIL, f"GitHub {repo.name}",
                                f"{repo.ref} 접근 가능" if good else f"접근 실패: {out.stderr.strip()[:120]}"))
        except Exception as exc:
            checks.append(Check(FAIL, f"GitHub {repo.name}", str(exc)[:120]))
    if s.notion_token and s.notion_root_page_id:
        try:
            import httpx

            r = httpx.get(f"https://api.notion.com/v1/pages/{s.notion_root_page_id}",
                          headers={"Authorization": f"Bearer {s.notion_token.get_secret_value()}", "Notion-Version": "2022-06-28"},
                          timeout=20)
            checks.append(Check(OK if r.status_code == 200 else FAIL, "노션",
                                "최상위 페이지 접근 가능" if r.status_code == 200 else f"HTTP {r.status_code} — 페이지를 통합에 연결했는지 확인"))
        except Exception as exc:
            checks.append(Check(FAIL, "노션", str(exc)[:120]))
    return checks


def run(online: bool) -> int:
    s = Settings()
    checks = offline_checks(s) + (online_checks(s) if online else [])
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"{c.status} {c.name.ljust(width)}  {c.detail}")
    failed = [c for c in checks if c.status == FAIL and c.required]
    print()
    if failed:
        print(f"필수 항목 {len(failed)}개를 해결해야 실행할 수 있어요.")
        return 1
    print("실행 준비 완료." + ("" if online else " 연결까지 확인하려면: askcode doctor --online"))
    if os.environ.get("ANTHROPIC_BASE_URL"):
        print("참고: ANTHROPIC_BASE_URL이 설정돼 있어 Claude 호출이 다른 엔드포인트로 갈 수 있어요.")
    return 0
