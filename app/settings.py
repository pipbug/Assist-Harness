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

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
