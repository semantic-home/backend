from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),             # lädt .env, wenn vorhanden
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    api_prefix: str = Field(default="/api/v1")
    semantic_home_beta_key: str = Field(default="")
    database_url: str = "postgresql://semantic_home:semantic_home@127.0.0.1:55432/semantic_home"
    auth_cookie_secure: bool = False
    registration_key: str = ""
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # weitere Felder nach Bedarf, z. B. timeouts, topics, log-level …

# eine sofort benutzbare, globale Instanz
settings = Settings()
