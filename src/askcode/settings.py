"""환경 설정. 비밀값은 .env(또는 환경변수)에서만 읽고 코드·레포에 두지 않는다."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ASKCODE_",
        extra="ignore",
    )

    # 경로
    data_dir: Path = Field(default=Path("data"), description="클론·DB 저장 위치")
    config_dir: Path = Field(default=Path("config"))

    # Claude
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model_large: str = "claude-sonnet-5-5"
    model_small: str = "claude-haiku-5-5"
    agent_max_turns: int = 12
    agent_max_budget_usd: float = 0.5

    # GitHub — GitHub App 우선, 없으면 읽기 전용 토큰(개발용)
    github_app_id: str | None = None
    github_app_private_key_path: Path | None = None
    github_app_installation_id: str | None = None
    github_token: SecretStr | None = None

    # Slack (Socket Mode)
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    escalation_channel: str | None = Field(default=None, description="'개발자에게 확인 요청'을 올릴 채널 ID")

    # 단순 호출 비용 추정용 단가 (USD / 100만 토큰). 현재 가격표에 맞게 설정
    price_small_input: float = 0.10  # claude-haiku-5-5, USD / 100만 토큰
    price_small_output: float = 0.50

    # 노션 (봇 전용 내부 통합, 읽기 전용)
    notion_token: SecretStr | None = None
    notion_root_page_id: str | None = None

    # 슬랙 소스 채널 (공개 채널 ID, 쉼표 구분) · 기간
    slack_source_channels: str = ""
    slack_source_days: int = 90

    # 도구 상한
    search_max_results: int = 20
    read_max_lines: int = 200

    @property
    def db_path(self) -> Path:
        return self.data_dir / "askcode.sqlite3"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"


@lru_cache
def get_settings() -> Settings:
    return Settings()
