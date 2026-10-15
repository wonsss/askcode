"""경로 제외 규칙. 인덱싱·검색·읽기 모든 단계에서 같은 규칙을 적용한다(이중 안전장치)."""

from __future__ import annotations

import re
from functools import lru_cache

# 어떤 설정과도 무관하게 항상 차단하는 경로(비밀값·키 파일)
ALWAYS_BLOCK = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p8",
    "**/*.p12",
    "**/id_rsa*",
    "**/.git/**",
)
ALWAYS_ALLOW = ("**/.env.example",)


def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[: m.start()], pattern[m.end() :]
    out: list[str] = []
    for option in m.group(1).split(","):
        out.extend(_expand_braces(head + option + tail))
    return out


@lru_cache(maxsize=1024)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """gitignore 스타일 글롭(**, *, ?)을 정규식으로 바꾼다. `**/`는 0개 이상의 디렉터리."""
    i, out = 0, ["^"]
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    for pattern in patterns:
        for expanded in _expand_braces(pattern):
            if _glob_to_regex(expanded).match(path):
                return True
    return False


class PathFilter:
    def __init__(self, exclude: tuple[str, ...], include_exceptions: tuple[str, ...] = ()):
        self.exclude = tuple(exclude) + ALWAYS_BLOCK
        self.include_exceptions = tuple(include_exceptions) + ALWAYS_ALLOW

    def allowed(self, path: str) -> bool:
        while path.startswith("./"):
            path = path[2:]
        if not path or path.startswith("/") or ".." in path.split("/"):
            return False
        if matches(path, self.include_exceptions):
            return True
        return not matches(path, self.exclude)
