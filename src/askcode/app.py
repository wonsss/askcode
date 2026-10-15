"""조립 지점(composition root). 구현체 선택은 여기서만 한다."""

from __future__ import annotations

from dataclasses import dataclass

from askcode.agent.claude_runner import ClaudeAgentRunner
from askcode.config import RepoConfig, load_repos
from askcode.indexing.code_index import IndexStats, build_code_index
from askcode.indexing.repo_sync import RepoSync, SyncResult
from askcode.pipeline.answer import AnswerService
from askcode.settings import Settings
from askcode.store.db import Store
from askcode.tools.code_tools import CodeTools


@dataclass
class Container:
    settings: Settings
    store: Store
    repo: RepoConfig
    sync: RepoSync
    service: AnswerService

    def refresh(self) -> tuple[SyncResult, IndexStats | None]:
        """레포를 동기화하고 바뀐 것이 있으면 인덱스를 다시 만든다."""
        result = self.sync.sync(self.repo)
        stats = None
        if result.full or result.changed_files or self.store.file_count(self.repo.name) == 0:
            stats = build_code_index(self.store, self.repo, self.sync.path_for(self.repo))
        self.store.set_repo_state(self.repo.name, result.commit_sha)
        return result, stats


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    store = Store(settings.db_path)
    repo = load_repos(settings.config_dir)[0]  # PoC는 레포 하나
    sync = RepoSync(settings)
    repo_path = sync.path_for(repo)
    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    runner = ClaudeAgentRunner(model=settings.model_large, api_key=api_key)
    tools = CodeTools(store, repo, repo_path, settings.search_max_results, settings.read_max_lines)
    service = AnswerService(store, repo, repo_path, runner, tools, settings.agent_max_turns, settings.agent_max_budget_usd)
    return Container(settings, store, repo, sync, service)
