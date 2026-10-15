"""Application settings loaded from environment variables."""

import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_db_url_for_asyncpg(url: str) -> str:
    """
    Normalize any PostgreSQL URL (local, Aiven, Supabase, Neon) for asyncpg.
    - Converts postgres:// or postgresql:// to postgresql+asyncpg://
    - Converts sslmode=... to ssl=... because asyncpg expects 'ssl', not 'sslmode'
    """
    clean_url = url.strip()
    if clean_url.startswith("postgres://"):
        clean_url = "postgresql+asyncpg://" + clean_url[len("postgres://"):]
    elif clean_url.startswith("postgresql://"):
        clean_url = "postgresql+asyncpg://" + clean_url[len("postgresql://"):]
    elif clean_url.startswith("postgresql+psycopg://"):
        clean_url = "postgresql+asyncpg://" + clean_url[len("postgresql+psycopg://"):]
    elif not clean_url.startswith("postgresql+asyncpg://"):
        clean_url = "postgresql+asyncpg://" + clean_url

    # asyncpg expects 'ssl' parameter, whereas Aiven and libpq provide 'sslmode'
    clean_url = re.sub(r"([?&])sslmode=([a-zA-Z0-9_-]+)", r"\1ssl=\2", clean_url)
    return clean_url


def normalize_db_url_for_sync(url: str) -> str:
    """
    Normalize any PostgreSQL URL for synchronous psycopg (used in importer & setup).
    - Converts postgres:// or postgresql+asyncpg:// to postgresql://
    - Ensures sslmode is preserved for libpq / psycopg
    """
    clean_url = url.strip()
    if clean_url.startswith("postgres://"):
        clean_url = "postgresql://" + clean_url[len("postgres://"):]
    elif clean_url.startswith("postgresql+asyncpg://"):
        clean_url = "postgresql://" + clean_url[len("postgresql+asyncpg://"):]
    elif clean_url.startswith("postgresql+psycopg://"):
        clean_url = "postgresql://" + clean_url[len("postgresql+psycopg://"):]

    clean_url = re.sub(r"([?&])ssl=([a-zA-Z0-9_-]+)", r"\1sslmode=\2", clean_url)
    return clean_url


class Settings(BaseSettings):
    """Central configuration for the medicine search backend.

    Supports local PostgreSQL and managed cloud services like Aiven.
    Paste your Aiven Service URI directly into DATABASE_URL.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database URL: can be local, Aiven (postgres://avnadmin:password@host:port/defaultdb?sslmode=require), etc.
    DATABASE_URL: str = (
        "postgresql+asyncpg://medicine:medicine_dev@localhost:5432/medicine_search"
    )
    DATABASE_URL_SYNC: str | None = None

    # Application
    APP_NAME: str = "Medicine Search API"
    DEBUG: bool = False

    @property
    def async_database_url(self) -> str:
        """Returns connection URL formatted for asyncpg (with SSL translation)."""
        return normalize_db_url_for_asyncpg(self.DATABASE_URL)

    @property
    def sync_database_url(self) -> str:
        """Returns connection URL formatted for psycopg."""
        if self.DATABASE_URL_SYNC:
            return normalize_db_url_for_sync(self.DATABASE_URL_SYNC)
        return normalize_db_url_for_sync(self.DATABASE_URL)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
