"""레포 레지스트리(config/repos.yaml) 로딩."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RepoConfig:
    name: str
    url: str
    ref: str = "main"
    description: str = ""
    exclude: tuple[str, ...] = ()
    include_exceptions: tuple[str, ...] = ()
    context_files: tuple[str, ...] = field(default_factory=tuple)
    """시스템 프롬프트에 레포 개요로 넣을 파일(예: CLAUDE.md)."""
    public_summary: str = ""
    """외부 조사 프롬프트 첫 줄에 넣는 서비스 소개. 사외로 나가므로 공개해도 되는 말만 쓴다."""
    external_blocklist: tuple[str, ...] = ()
    """외부 조사 프롬프트에 나오면 그 항목을 빼는 내부 용어(레포 이름은 자동 포함)."""

    @property
    def web_base(self) -> str:
        """인용 링크용 GitHub 웹 주소."""
        return self.url.removesuffix(".git")


def load_repos(config_dir: Path) -> list[RepoConfig]:
    raw = yaml.safe_load((config_dir / "repos.yaml").read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults", {})
    repos: list[RepoConfig] = []
    for item in raw.get("repos", []):
        exclude = [*defaults.get("exclude", []), *item.get("exclude", [])]
        include_exceptions = [*defaults.get("include_exceptions", []), *item.get("include_exceptions", [])]
        repos.append(
            RepoConfig(
                name=item["name"],
                url=item["url"],
                ref=item.get("ref", "main"),
                description=item.get("description", ""),
                exclude=tuple(exclude),
                include_exceptions=tuple(include_exceptions),
                context_files=tuple(item.get("context_files", [])),
                public_summary=item.get("public_summary", ""),
                external_blocklist=tuple(item.get("external_blocklist", [])),
            )
        )
    if not repos:
        raise ValueError("config/repos.yaml에 레포가 없습니다.")
    return repos
