"""
Resilient Async SQLAlchemy engine, session factory, and FastAPI dependency.

Supports:
- Primary Database: PostgreSQL (Aiven, Neon, Supabase, Vercel Postgres, Local Docker)
- Fallback Database: SQLite (aiosqlite) with automatic failover and starter seeding
"""

import asyncio
import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.db.base import Base
# Import models so Base.metadata knows about all tables
import app.db.models.catalog  # noqa: F401

logger = logging.getLogger("medicine_search.db")

settings = get_settings()


class DatabaseManager:
    """Manages primary PostgreSQL connection and SQLite fallback."""

    def __init__(self):
        self._primary_engine: AsyncEngine | None = None
        self._fallback_engine: AsyncEngine | None = None
        self._active_engine: AsyncEngine | None = None
        self._active_dialect: str = "postgresql"
        self._is_fallback: bool = False
        self._initialized: bool = False
        self._lock = asyncio.Lock()

    @property
    def active_engine(self) -> AsyncEngine:
        if self._active_engine is None:
            self._active_engine = self._create_initial_engine()
        return self._active_engine

    @property
    def active_dialect(self) -> str:
        return self._active_dialect

    @property
    def is_fallback(self) -> bool:
        return self._is_fallback

    def _create_initial_engine(self) -> AsyncEngine:
        current_settings = get_settings()
        postgres_url = current_settings.primary_postgres_url
        if postgres_url:
            self._active_dialect = "postgresql"
            self._is_fallback = False
            return create_async_engine(
                postgres_url,
                echo=current_settings.DEBUG,
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=5,
                pool_recycle=300,
            )
        else:
            self._active_dialect = "sqlite"
            self._is_fallback = True
            return create_async_engine(
                current_settings.fallback_sqlite_url,
                echo=current_settings.DEBUG,
            )

    async def initialize(self) -> str:
        """
        Verify primary PostgreSQL connection, or initialize SQLite fallback.
        Returns the active dialect ('postgresql' or 'sqlite').
        """
        async with self._lock:
            if self._initialized:
                return self._active_dialect

            current_settings = get_settings()
            postgres_url = current_settings.primary_postgres_url
            if postgres_url:
                try:
                    logger.info("[DB] Attempting connection to Primary PostgreSQL...")
                    test_engine = create_async_engine(
                        postgres_url,
                        echo=current_settings.DEBUG,
                        pool_pre_ping=True,
                        pool_size=2,
                        max_overflow=5,
                        pool_recycle=300,
                        connect_args={"timeout": 5} if "asyncpg" in postgres_url else {},
                    )

                    async def _verify():
                        async with test_engine.begin() as conn:
                            await conn.execute(text("SELECT 1"))
                            try:
                                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                            except Exception as ext_err:
                                logger.warning(f"[DB] pg_trgm check: {ext_err}")

                    await asyncio.wait_for(_verify(), timeout=6.0)

                    self._primary_engine = test_engine
                    self._active_engine = test_engine
                    self._active_dialect = "postgresql"
                    self._is_fallback = False
                    self._initialized = True
                    logger.info("[DB] ✓ Primary PostgreSQL connected successfully.")
                    return "postgresql"
                except Exception as e:
                    logger.warning(
                        f"[DB] Primary PostgreSQL connection failed ({e}). "
                        f"Switching to SQLite fallback..."
                    )

            # Fallback to SQLite
            await self._setup_sqlite_fallback()
            self._initialized = True
            return "sqlite"

    async def _setup_sqlite_fallback(self) -> None:
        """Initialize SQLite database with schema and starter data."""
        current_settings = get_settings()
        sqlite_url = current_settings.fallback_sqlite_url
        logger.info(f"[DB] Initializing SQLite fallback database at: {sqlite_url}")

        fallback_engine = create_async_engine(
            sqlite_url,
            echo=settings.DEBUG,
        )

        # Create all tables in SQLite
        async with fallback_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Seed starter data if empty
        from app.db.fallback_seed import seed_fallback_sqlite_if_empty
        session_factory = async_sessionmaker(
            bind=fallback_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            seeded = await seed_fallback_sqlite_if_empty(session)
            logger.info(f"[DB] SQLite fallback ready with {seeded} medicines.")

        self._fallback_engine = fallback_engine
        self._active_engine = fallback_engine
        self._active_dialect = "sqlite"
        self._is_fallback = True

    def get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(
            bind=self.active_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def dispose(self) -> None:
        if self._primary_engine:
            await self._primary_engine.dispose()
        if self._fallback_engine:
            await self._fallback_engine.dispose()
        if self._active_engine and self._active_engine not in (self._primary_engine, self._fallback_engine):
            await self._active_engine.dispose()


db_manager = DatabaseManager()


class EngineProxy:
    """Proxy object so `from app.db.session import engine` remains backward-compatible."""
    def __getattr__(self, name):
        return getattr(db_manager.active_engine, name)


engine = EngineProxy()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    factory = db_manager.get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
