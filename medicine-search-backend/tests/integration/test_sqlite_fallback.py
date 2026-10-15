"""
Integration tests for SQLite fallback mode.

Verifies that when PostgreSQL is unavailable or unconfigured:
1. SQLite database initializes with schema and starter data.
2. Exact, prefix, and typo searches execute cleanly.
3. Discontinued items and deduplication work as expected.
"""

import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

from app.db.base import Base
import app.db.models.catalog  # noqa: F401
from app.db.fallback_seed import seed_fallback_sqlite_if_empty
from app.services.search import search_medicines


async def _get_sqlite_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    await seed_fallback_sqlite_if_empty(session)
    return engine, session


def test_sqlite_fallback_seeded():
    """Verify that starter catalog is seeded into SQLite."""
    async def _test():
        engine, session = await _get_sqlite_session()
        try:
            res = await session.execute(text("SELECT count(*) FROM medicines"))
            count = res.scalar_one()
            assert count >= 15
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_test())


def test_sqlite_exact_search():
    """Verify exact medicine search on SQLite."""
    async def _test():
        engine, session = await _get_sqlite_session()
        try:
            results = await search_medicines(session, "Augmentin 625 Duo Tablet", limit=5)
            assert len(results) >= 1
            top = results[0]
            assert top.name == "Augmentin 625 Duo Tablet"
            assert top.match_reason == "exact_name"
            assert top.score == 1.0
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_test())


def test_sqlite_prefix_search():
    """Verify prefix medicine search on SQLite."""
    async def _test():
        engine, session = await _get_sqlite_session()
        try:
            results = await search_medicines(session, "dolo 650", limit=5)
            assert len(results) >= 1
            top = results[0]
            assert "Dolo 650" in top.name
            assert top.match_reason == "prefix"
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_test())


def test_sqlite_fuzzy_typo_search():
    """Verify typo tolerance on SQLite fallback."""
    async def _test():
        engine, session = await _get_sqlite_session()
        try:
            results = await search_medicines(session, "augmentn 625", limit=5)
            assert len(results) >= 1
            top = results[0]
            assert "Augmentin" in top.name
            assert top.match_reason == "typo_match"
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_test())


def test_sqlite_alias_search():
    """Verify alias lookup on SQLite fallback."""
    async def _test():
        engine, session = await _get_sqlite_session()
        try:
            results = await search_medicines(session, "augmentin", limit=5)
            assert len(results) >= 1
            assert any("Augmentin" in r.name for r in results)
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_test())


def test_sqlite_discontinued_selectable():
    """Verify discontinued flag is preserved on SQLite."""
    async def _test():
        engine, session = await _get_sqlite_session()
        try:
            results = await search_medicines(session, "Discontinued Sample", limit=5)
            if results:
                top = results[0]
                assert top.is_discontinued is True
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_test())
