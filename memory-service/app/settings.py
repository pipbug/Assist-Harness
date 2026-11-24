from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    postgres_host: str = "memory-postgres"
    postgres_port: int = 5432
    postgres_user: str = "memory"
    postgres_password: str = ""
    postgres_db: str = "kira_memory"

    memory_api_key: str = ""
    memory_ui_password: str = ""
    session_secret: str = ""
    openai_api_key: str = ""
    openai_embed_model: str = "text-embedding-3-small"
    default_user_id: str = "kira"

    # Phase 1: extraction and dedup
    openai_llm_model: str = "gpt-5.4-nano"
    openai_reasoning_effort: str = "low"
    extraction_policy_path: str = "/config/extraction_policy.txt"
    # cosine similarity: at or above dedup_noop a new fact is treated as already known (keep this
    # very high: a value change such as "lights at 40%" -> "30%" still scores ~0.97);
    # between dedup_candidate and dedup_noop the LLM decides ADD / UPDATE / NOOP
    dedup_noop: float = 0.99
    dedup_candidate: float = 0.70
    backfill_interval_s: int = 600

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
