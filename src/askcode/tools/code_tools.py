"""읽기 전용 코드 도구. 인덱스에 등록된 파일만 다루고, 결과 크기에 상한을 둔다.

도구가 실제로 보여준 구간은 ToolSession에 기록되며, 답변의 인용이 확인한 범위 안에
있는지 검증하는 근거가 된다.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from askcode.config import RepoConfig
from askcode.indexing.filters import PathFilter, matches
from askcode.store.db import Store

MAX_LINE_CHARS = 240
# 값이 코드가 아니라 환경변수·원격 설정·플래그에서 오는 줄
CONFIG_RE = re.compile(
    r"process\.env|import\.meta\.env|EXPO_PUBLIC_|NEXT_PUBLIC_|configService\.get|ConfigService|getEnv\(|"
    r"remoteConfig|featureFlag|FeatureFlag|isFeatureEnabled|Constants\.expoConfig"
)
CONFIG_MARK = "  ⟵⚙ 설정·환경값 의존"


@dataclass
class ToolSession:
    """한 질문 처리 동안 도구가 보여준 근거 구간."""

    seen: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    scope_globs: tuple[str, ...] | None = None
    """스코핑이 정한 허용 경로. None이면 레포 전체."""
    widened: bool = False
    config_dependent: set[str] = field(default_factory=set)
    dirs: set[str] = field(default_factory=set)
    """list_dir로 실제 본 폴더(앞뒤 / 없이). 폴더 경로 근거 검증에 쓴다."""

    def in_scope(self, path: str) -> bool:
        return self.widened or self.scope_globs is None or matches(path, self.scope_globs)

    def mark(self, path: str, start: int, end: int) -> None:
        self.seen.setdefault(path, []).append((start, end))

    def covers(self, path: str, start: int, end: int, slack: int = 3) -> bool:
        return any(s - slack <= start and end <= e + slack for s, e in self.seen.get(path, []))


class CodeTools:
    def __init__(self, store: Store, repo: RepoConfig, repo_path: Path, max_results: int = 20, max_lines: int = 200):
        self.store = store
        self.repo = repo
        self.repo_path = repo_path.resolve()
        self.filter = PathFilter(repo.exclude, repo.include_exceptions)
        self.max_results = max_results
        self.max_lines = max_lines

    def _check(self, path: str, session: ToolSession | None = None) -> str | None:
        """허용되지 않은 경로면 거부 사유를 돌려준다."""
        if not self.filter.allowed(path):
            return "접근이 허용되지 않은 경로입니다."
        full = (self.repo_path / path).resolve()
        if not str(full).startswith(str(self.repo_path) + "/"):
            return "레포 밖 경로입니다."
        if not self.store.file_info(self.repo.name, path):
            return "인덱스에 없는 파일입니다. code_search로 정확한 경로를 먼저 찾으세요."
        if session is not None and not session.in_scope(path):
            return "질문 범위 밖 경로입니다. 꼭 필요하면 widen_scope를 한 번 호출하세요."
        return None

    def search(self, session: ToolSession, query: str, path_glob: str | None = None, fixed: bool = True) -> str:
        if not query.strip():
            return "검색어가 비어 있습니다."
        cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "--smart-case",
               "--max-count", "5", "--max-columns", str(MAX_LINE_CHARS), "--max-columns-preview"]
        if fixed:
            cmd.append("--fixed-strings")
        # rg 글롭은 뒤에 온 것이 우선한다: 포함 범위를 먼저, 제외 규칙을 마지막에 둔다
        if path_glob:
            cmd += ["-g", path_glob]
        elif session.scope_globs and not session.widened:
            for g in session.scope_globs:
                cmd += ["-g", g]
        for pattern in (*self.repo.exclude, "**/.env", "**/.env.*"):
            cmd += ["-g", f"!{pattern}"]
        cmd += ["-e", query, "."]
        proc = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, text=True, timeout=30)
        if proc.returncode not in (0, 1):
            return f"검색 오류: {proc.stderr.strip()[:200]}"
        hits: list[str] = []
        files: set[str] = set()
        for line in proc.stdout.splitlines():
            path, _, rest = line.partition(":")
            lineno, _, text = rest.partition(":")
            path = path.removeprefix("./")
            if not lineno.isdigit() or self._check(path, session):
                continue
            n = int(lineno)
            session.mark(path, n, n)
            files.add(path)
            mark = CONFIG_MARK if CONFIG_RE.search(text) else ""
            if mark:
                session.config_dependent.add(path)
            hits.append(f"{path}:{n}: {text.strip()[:MAX_LINE_CHARS]}{mark}")
            if len(hits) >= self.max_results:
                break
        if not hits:
            scoped = " (질문 범위 안에서만 찾았습니다. 범위 밖이 의심되면 widen_scope)" if session.scope_globs and not session.widened else ""
            return f"'{query}' 결과 없음{scoped}. 다른 표현(영문 식별자, 한국어 화면 문구)으로 다시 검색해 보세요."
        more = " (상한에 도달해 일부만 표시. path_glob으로 범위를 좁히세요)" if len(hits) >= self.max_results else ""
        return f"{len(hits)}건, 파일 {len(files)}개{more}\n" + "\n".join(hits)

    def read(self, session: ToolSession, path: str, start: int = 1, end: int | None = None) -> str:
        path = path.removeprefix("./")
        reason = self._check(path, session)
        if reason:
            return f"{path}: {reason}"
        info = self.store.file_info(self.repo.name, path)
        total = info["lines"]
        start = max(1, start)
        end = min(total, end if end else start + self.max_lines - 1, start + self.max_lines - 1)
        if start > total:
            return f"{path}: 파일은 {total}줄입니다."
        with (self.repo_path / path).open(encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        out_lines = []
        for i in range(start, end + 1):
            line = lines[i - 1][:400]
            mark = CONFIG_MARK if CONFIG_RE.search(line) else ""
            if mark:
                session.config_dependent.add(path)
            out_lines.append(f"{i:>5}  {line}{mark}")
        body = "\n".join(out_lines)
        session.mark(path, start, end)
        tail = f"\n… (전체 {total}줄, 다음 구간은 start={end + 1})" if end < total else ""
        return f"{path} [{start}-{end} / {total}줄]\n{body}{tail}"

    def list_dir(self, session: ToolSession, prefix: str) -> str:
        prefix = prefix.strip().strip("/")
        rows = self.store.execute(
            "SELECT path, lines FROM files WHERE repo=? AND path LIKE ? ORDER BY path",
            (self.repo.name, f"{prefix}/%" if prefix else "%"),
        ).fetchall()
        if not rows:
            return f"'{prefix}' 아래 인덱싱된 파일이 없습니다."
        # 바로 아래 단계만 묶어서 보여준다 (디렉터리 전체 나열 방지)
        depth = len(prefix.split("/")) if prefix else 0
        children: dict[str, int] = {}
        for r in rows:
            parts = r["path"].split("/")
            key = "/".join(parts[: depth + 1]) + ("/" if len(parts) > depth + 1 else "")
            children[key] = children.get(key, 0) + 1
        lines = [f"{k}  ({v}개 파일)" if k.endswith("/") else k for k, v in sorted(children.items())]
        session.dirs.add(prefix)
        session.dirs.update(k.rstrip("/") for k in children if k.endswith("/"))  # 목록에 보인 하위 폴더도 본 것
        return f"{prefix or '(루트)'} — {len(children)}개 항목\n" + "\n".join(lines[:150])
