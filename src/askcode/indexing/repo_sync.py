"""GitHub 레포 동기화. 읽기 전용 얕은 클론을 유지하고, fetch 때 바뀐 파일 목록을 돌려준다.

인증 우선순위: GitHub App 설치 토큰 → 읽기 전용 토큰 → 인증 없음(공개 레포).
토큰은 git 명령마다 http.extraHeader로만 전달해 .git/config에 남기지 않는다.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jwt

from askcode.config import RepoConfig
from askcode.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    repo: str
    commit_sha: str
    previous_sha: str | None
    changed_files: list[str] = field(default_factory=list)
    full: bool = False


class GitHubAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str | None:
        s = self.settings
        if s.github_app_id and s.github_app_private_key_path and s.github_app_installation_id:
            if self._token and time.time() < self._expires_at - 120:
                return self._token
            now = int(time.time())
            app_jwt = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": s.github_app_id},
                s.github_app_private_key_path.read_text(),
                algorithm="RS256",
            )
            resp = httpx.post(
                f"https://api.github.com/app/installations/{s.github_app_installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
                timeout=30,
            )
            resp.raise_for_status()
            self._token = resp.json()["token"]
            self._expires_at = time.time() + 3600
            return self._token
        if s.github_token:
            return s.github_token.get_secret_value()
        return None

    def git_config_args(self) -> list[str]:
        token = self.token()
        if not token:
            return []
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]


def _git(args: list[str], cwd: Path | None = None, auth: list[str] | None = None, timeout: int = 900) -> str:
    cmd = ["git", *(auth or []), *args]
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        # 오류 메시지에 인증 헤더가 섞이지 않도록 명령 인자는 남기지 않는다
        raise RuntimeError(f"git {args[0]} 실패: {result.stderr.strip()[:500]}")
    return result.stdout


class RepoSync:
    def __init__(self, settings: Settings, auth: GitHubAuth | None = None):
        self.settings = settings
        self.auth = auth or GitHubAuth(settings)

    def path_for(self, repo: RepoConfig) -> Path:
        return self.settings.repos_dir / repo.name

    def tree(self, repo: RepoConfig) -> list[tuple[str, str]]:
        """현재 체크아웃의 (경로, git blob 해시) 목록."""
        out = _git(["ls-tree", "-r", "--full-tree", "HEAD"], cwd=self.path_for(repo))
        rows = []
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                rows.append((path, parts[2]))
        return rows

    def head(self, repo: RepoConfig) -> str | None:
        path = self.path_for(repo)
        if not (path / ".git").exists():
            return None
        return _git(["rev-parse", "HEAD"], cwd=path).strip()

    def sync(self, repo: RepoConfig) -> SyncResult:
        path = self.path_for(repo)
        auth = self.auth.git_config_args()
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            log.info("cloning %s@%s", repo.name, repo.ref)
            _git(["clone", "--depth", "1", "--branch", repo.ref, "--single-branch", repo.url, str(path)], auth=auth)
            sha = self.head(repo)
            assert sha
            return SyncResult(repo.name, sha, None, full=True)

        previous = self.head(repo)
        _git(["fetch", "--depth", "1", "origin", repo.ref], cwd=path, auth=auth)
        new_sha = _git(["rev-parse", "FETCH_HEAD"], cwd=path).strip()
        if new_sha == previous:
            return SyncResult(repo.name, new_sha, previous)
        try:
            changed = _git(["diff", "--name-only", previous or new_sha, new_sha], cwd=path).splitlines()
        except RuntimeError:
            changed, previous = [], None  # 이전 커밋 객체가 없으면 전체 재인덱싱
        _git(["reset", "--hard", new_sha], cwd=path)
        return SyncResult(repo.name, new_sha, previous, changed_files=changed, full=previous is None)
