"""Runtime configuration, read from KEYFINDER_* environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class Settings(BaseSettings):
    # env_ignore_empty: `KEYFINDER_API_KEY=` (common in .env files and compose `${VAR:-}`) means
    # "unset", not "the empty string", for every setting.
    model_config = SettingsConfigDict(
        env_prefix="KEYFINDER_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # Storage
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".keyfinder")
    cache_ttl_seconds: int = Field(default=86_400, ge=0)  # 0 disables the response cache

    # Outbound HTTP (politeness towards the stores)
    request_interval: float = Field(default=0.3, ge=0)  # min seconds between requests to one host
    max_concurrency: int = Field(default=4, ge=1, le=32)  # in-flight requests across all hosts
    request_timeout: float = Field(default=15.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    user_agent: str = DEFAULT_USER_AGENT

    # Web server
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    api_key: SecretStr | None = None  # when set, /api/* requires X-API-Key or Bearer token
    max_concurrent_runs: int = Field(default=2, ge=1, le=16)
    cors_origins: list[str] = Field(default_factory=list)

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "keyfinder.db"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "cache.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
