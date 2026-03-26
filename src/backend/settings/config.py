from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),             # lädt .env, wenn vorhanden
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    api_prefix: str = Field(default="/api/v1")
    app_env: Literal["dev", "prod"] = Field(default="prod")
    # weitere Felder nach Bedarf, z. B. timeouts, topics, log-level …

# eine sofort benutzbare, globale Instanz
settings = Settings()
