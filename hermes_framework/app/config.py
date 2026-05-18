from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    mcp_url: str = "http://localhost:7700/mcp/"
    state_db_path: Path = Path("./hermes_state.db")

    planner_model: str = "claude-sonnet-4-6"
    worker_model: str = "claude-sonnet-4-6"
    planner_effort: str = "high"
    planner_thinking: str = "adaptive"

    bulk_doc_threshold: int = 20

    sandbox_timeout_seconds: int = 120
    sandbox_bulk_timeout_seconds: int = 600  # BULK_TOOL_CALL / CODE_TRANSFORM tasks
    sandbox_rss_mb: int = 512

    api_host: str = "0.0.0.0"
    api_port: int = 8080


settings = Settings()
