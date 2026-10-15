"""조립 지점(composition root). 구현체 선택은 여기서만 한다."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from askcode.agent.claude_runner import ClaudeAgentRunner
from askcode.config import RepoConfig, load_repos
from askcode.indexing.code_index import build_code_index
from askcode.indexing.docs_index import index_repo_docs, store_virtual_docs
from askcode.indexing.repo_sync import RepoSync
from askcode.indexing.structure import build_structure
from askcode.llm.complete import LLM
from askcode.pipeline.answer import AnswerService
from askcode.pipeline.scoping import DomainMap, Scoper, load_domain_map
from askcode.settings import Settings
from askcode.store.db import Store
from askcode.tools.code_tools import CodeTools
from askcode.tools.doc_tools import DocTools
from askcode.tools.structure_tools import StructureTools

log = logging.getLogger(__name__)


@dataclass
class RefreshReport:
    commit: str
    previous: str | None
    changed_files: int
    reindexed: bool
    stats: dict = field(default_factory=dict)


@dataclass
class Container:
    settings: Settings
    store: Store
    repo: RepoConfig
    sync: RepoSync
    service: AnswerService
    llm: LLM
    domain_map: DomainMap

    def refresh(self, *, external: bool = True, force: bool = False) -> RefreshReport:
        """레포 동기화 → (변경 시) 코드 인덱스·구조 맵·레포 문서 재생성 → 노션·슬랙 수집."""
        result = self.sync.sync(self.repo)
        root = self.sync.path_for(self.repo)
        reindex = force or result.full or bool(result.changed_files) or self.store.file_count(self.repo.name) == 0
        stats: dict = {}
        if reindex:
            idx = build_code_index(self.store, self.repo, root)
            stats["code"] = idx.__dict__
            files = [r["path"] for r in self.store.execute("SELECT path FROM files WHERE repo=?", (self.repo.name,))]
            stats["structure"] = build_structure(self.store, self.repo.name, root, files)
            stats["repo_docs"] = index_repo_docs(self.store, root, files, self.repo.web_base, result.commit_sha,
                                                 self.service.code_tools.filter)
        self.store.set_repo_state(self.repo.name, result.commit_sha)
        if external:
            stats.update(self.refresh_external())
        return RefreshReport(result.commit_sha, result.previous_sha, len(result.changed_files), reindex, stats)

    def refresh_external(self) -> dict:
        s, out = self.settings, {}
        if s.notion_token and s.notion_root_page_id:
            from askcode.indexing.notion_source import NotionSource
            try:
                items = NotionSource(s.notion_token.get_secret_value(), s.notion_root_page_id).collect()
                out["notion_pages"] = store_virtual_docs(self.store, "notion", items)
            except Exception as exc:
                log.exception("notion sync failed")
                out["notion_error"] = str(exc)[:200]
        channels = [c.strip() for c in s.slack_source_channels.split(",") if c.strip()]
        if s.slack_bot_token and channels:
            from askcode.indexing.slack_source import SlackSource
            try:
                items = SlackSource(s.slack_bot_token.get_secret_value(), channels, s.slack_source_days).collect()
                out["slack_threads"] = store_virtual_docs(self.store, "slack", items)
            except Exception as exc:
                log.exception("slack source sync failed")
                out["slack_error"] = str(exc)[:200]
        return out


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    store = Store(settings.db_path)
    repo = load_repos(settings.config_dir)[0]  # PoC는 레포 하나
    sync = RepoSync(settings)
    repo_path = sync.path_for(repo)
    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    llm = LLM(settings)
    domain_map = load_domain_map(settings.config_dir)
    runner = ClaudeAgentRunner(model=settings.model_large, api_key=api_key)
    code_tools = CodeTools(store, repo, repo_path, settings.search_max_results, settings.read_max_lines)
    service = AnswerService(
        store, repo, repo_path, runner, code_tools, settings.agent_max_turns, settings.agent_max_budget_usd,
        scoper=Scoper(llm, domain_map), domain_map=domain_map, doc_tools=DocTools(store),
        structure_tools=StructureTools(store, repo.name, repo_path),
        models={"small": settings.model_small, "large": settings.model_large},
        efforts={"small": settings.agent_effort_small or None, "large": settings.agent_effort_large or None},
    )
    return Container(settings, store, repo, sync, service, llm, domain_map)
