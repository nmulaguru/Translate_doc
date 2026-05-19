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

    # Non-bulk tasks (TOOL_CALL, RAG_QUERY) — simple deadline.
    sandbox_timeout_seconds: int = 120
    # Bulk tasks (CODE_TRANSFORM, BULK_TOOL_CALL) — heartbeat-based watchdog.
    # The "hard ceiling" is huge (24h) on purpose: real 1M-doc translations
    # take hours. The heartbeat protects against hung processes — if the
    # sandbox emits NO marker for `sandbox_heartbeat_timeout_seconds`, it's
    # killed. That's the actual liveness check, not wall-clock duration.
    sandbox_bulk_timeout_seconds: int = 86400        # 24h hard ceiling
    sandbox_heartbeat_timeout_seconds: int = 300     # 5min silence → dead
    sandbox_rss_mb: int = 512

    # Resume orphan EXECUTING sessions on process startup.
    resume_interrupted_on_startup: bool = True

    # Webhook delivery on session.completed / session.error.
    webhook_timeout_seconds: float = 10.0
    webhook_max_retries: int = 2

    api_host: str = "0.0.0.0"
    api_port: int = 8080
    # External base URL the API is reachable at — used to render absolute
    # clickable artifact links in final answers.
    public_base_url: str = "http://localhost:8080"


settings = Settings()
