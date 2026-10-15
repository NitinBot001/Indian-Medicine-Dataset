"""
Medicine Search Backend — FastAPI Application

Main entry point. Configures CORS, mounts API routes, and provides health check.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.db.session import engine as async_engine
from app.api.v1.routes.search import router as search_router
from app.api.v1.routes.catalog import router as catalog_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Verify database connection and pg_trgm extension
    async with async_engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        # Ensure pg_trgm is available
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    yield
    await async_engine.dispose()


app = FastAPI(
    title="Medicine Fuzzy Search API",
    description="Fast typo-tolerant medicine search for pharmacy billing. "
                "Uses PostgreSQL pg_trgm for sub-100ms fuzzy matching across 253k+ medicines.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow all origins in development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routers
app.include_router(search_router, prefix="/api/v1")
app.include_router(catalog_router, prefix="/api/v1")


@app.get("/health")
async def health():
    """Basic health check."""
    return {"status": "ok"}
