"""Application settings loaded from environment variables."""

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_db_url_for_asyncpg(url: str) -> str:
    """
    Normalize any PostgreSQL URL (local, Aiven, Supabase, Neon) for asyncpg.
    - Converts postgres:// or postgresql:// to postgresql+asyncpg://
    - Converts sslmode=... to ssl=... because asyncpg expects 'ssl', not 'sslmode'
    """
    clean_url = url.strip()
    if clean_url.startswith("sqlite"):
        return clean_url

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
    if clean_url.startswith("sqlite"):
        return clean_url

    if clean_url.startswith("postgres://"):
        clean_url = "postgresql://" + clean_url[len("postgres://"):]
    elif clean_url.startswith("postgresql+asyncpg://"):
        clean_url = "postgresql://" + clean_url[len("postgresql+asyncpg://"):]
    elif clean_url.startswith("postgresql+psycopg://"):
        clean_url = "postgresql://" + clean_url[len("postgresql+psycopg://"):]

    clean_url = re.sub(r"([?&])ssl=([a-zA-Z0-9_-]+)", r"\1sslmode=\2", clean_url)
    return clean_url


def get_default_sqlite_url() -> str:
    """
    Determine default SQLite fallback URL.
    In Vercel / serverless environments, /tmp is the only writable directory.
    """
    custom = os.getenv("SQLITE_URL") or os.getenv("SQLITE_DATABASE_URL")
    if custom:
        if custom.startswith("sqlite:///"):
            return "sqlite+aiosqlite:///" + custom[len("sqlite:///"):]
        return custom

    custom_path = os.getenv("SQLITE_PATH")
    if custom_path:
        return f"sqlite+aiosqlite:///{custom_path}"

    # Check for writable /tmp (standard on AWS Lambda, Vercel, and Linux)
    if os.path.exists("/tmp") and os.access("/tmp", os.W_OK):
        return "sqlite+aiosqlite:////tmp/medicines_fallback.db"
    return "sqlite+aiosqlite:///medicines_fallback.db"


def find_postgres_url() -> str | None:
    """
    Detect PostgreSQL URL from standard environment variables:
    AIVEN_DATABASE_URL, POSTGRES_URL, DATABASE_URL, etc.
    """
    for key in ("AIVEN_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "DATABASE_URL"):
        val = os.getenv(key)
        if val:
            val_clean = val.strip().strip("'\"")
            if val_clean.startswith(("postgres://", "postgresql://", "postgresql+")):
                return val_clean
    return None


class Settings(BaseSettings):
    """Central configuration for the medicine search backend.

    Supports:
    1. Primary: Aiven, Neon, Supabase, or local PostgreSQL.
    2. Fallback: SQLite (via aiosqlite) when PostgreSQL is absent or unreachable.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database URLs
    DATABASE_URL: str | None = (
        "postgresql+asyncpg://medicine:medicine_dev@localhost:5432/medicine_search"
    )
    DATABASE_URL_SYNC: str | None = None
    AIVEN_DATABASE_URL: str | None = None
    POSTGRES_URL: str | None = None
    SQLITE_URL: str | None = None
    SQLITE_PATH: str | None = None

    # Application
    APP_NAME: str = "Medicine Search API"
    DEBUG: bool = False
    ALLOW_TINY_FALLBACK: bool = False

    @property
    def primary_postgres_url(self) -> str | None:
        """Returns primary PostgreSQL URL formatted for asyncpg (with SSL translation)."""
        # 1. Direct AIVEN_DATABASE_URL
        if self.AIVEN_DATABASE_URL and self.AIVEN_DATABASE_URL.strip():
            return normalize_db_url_for_asyncpg(self.AIVEN_DATABASE_URL)
        # 2. Vercel standard POSTGRES_URL
        if self.POSTGRES_URL and self.POSTGRES_URL.strip():
            return normalize_db_url_for_asyncpg(self.POSTGRES_URL)
        # 3. DATABASE_URL (if postgres)
        if self.DATABASE_URL and self.DATABASE_URL.strip().startswith(("postgres://", "postgresql://", "postgresql+")):
            return normalize_db_url_for_asyncpg(self.DATABASE_URL)
        # 4. Fallback search in os.environ
        detected = find_postgres_url()
        if detected:
            return normalize_db_url_for_asyncpg(detected)
        return None

    @property
    def fallback_sqlite_url(self) -> str:
        """Returns SQLite fallback URL (aiosqlite driver)."""
        if self.SQLITE_URL and self.SQLITE_URL.strip():
            return self.SQLITE_URL.strip()
        if self.DATABASE_URL and self.DATABASE_URL.strip().startswith("sqlite"):
            return self.DATABASE_URL.strip()
        return get_default_sqlite_url()

    @property
    def async_database_url(self) -> str:
        """Returns connection URL formatted for asyncpg (PostgreSQL) or aiosqlite (SQLite)."""
        primary = self.primary_postgres_url
        if primary:
            return primary
        return self.fallback_sqlite_url

    @property
    def sync_database_url(self) -> str:
        """Returns connection URL formatted for psycopg (or SQLite)."""
        if self.DATABASE_URL_SYNC:
            return normalize_db_url_for_sync(self.DATABASE_URL_SYNC)
        primary = self.primary_postgres_url
        if primary:
            return normalize_db_url_for_sync(primary)
        return self.fallback_sqlite_url


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
