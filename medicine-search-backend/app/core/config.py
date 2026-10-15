"""Application settings loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the medicine search backend.

    Values are read from environment variables (or a .env file located in
    the project root).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database -----------------------------------------------------------
    DATABASE_URL: str = (
        "postgresql+asyncpg://medicine:medicine_dev@localhost:5432/medicine_search"
    )
    DATABASE_URL_SYNC: str = (
        "postgresql+psycopg://medicine:medicine_dev@localhost:5432/medicine_search"
    )

    # --- Application --------------------------------------------------------
    APP_NAME: str = "Medicine Search API"
    DEBUG: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
