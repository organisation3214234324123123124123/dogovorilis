from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET_KEY = "dev-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_version: str = "dev"
    database_url: str = "postgresql+psycopg://leaderhelp:leaderhelp@localhost:5432/leaderhelp"
    redis_url: str = "redis://localhost:6379/0"
    live_bus: Literal["memory", "redis"] = "redis"  # при пустом REDIS_URL - memory (FR-14)
    secret_key: str = DEFAULT_SECRET_KEY
    cors_origins: list[str] = ["http://localhost:5173"]
    admin_token: str = ""
    public_base_url: str = "http://localhost:8080"

    llm_api_key: str = ""
    llm_base_url: str = "https://llm.api.cloud.yandex.net/v1"
    llm_model: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "noreply@localhost"

    @model_validator(mode="after")
    def check_secret_key(self) -> "Settings":
        if not self.redis_url:
            self.live_bus = "memory"
        weak = self.secret_key == DEFAULT_SECRET_KEY or len(self.secret_key) < 32
        if self.app_version != "dev" and weak:
            raise ValueError("SECRET_KEY must be set to a value of 32+ characters outside dev")
        if self.app_version != "dev" and 0 < len(self.admin_token) < 32:
            raise ValueError("ADMIN_TOKEN must be empty or 32+ characters outside dev")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
