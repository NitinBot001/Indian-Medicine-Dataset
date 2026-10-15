import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import db_manager
from app.api.v1.routes.search import router as search_router
from app.api.v1.routes.catalog import router as catalog_router

logger = logging.getLogger("medicine_search")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle with resilient primary & fallback initialization."""
    try:
        active_dialect = await db_manager.initialize()
        logger.info(f"Medicine Search API ready (active database: {active_dialect})")
    except Exception as e:
        logger.error(f"Error during database initialization: {e}")
    yield
    await db_manager.dispose()


app = FastAPI(
    title="Medicine Fuzzy Search API",
    description="Fast typo-tolerant medicine search for pharmacy billing. "
                "Uses PostgreSQL pg_trgm for sub-100ms fuzzy matching across 253k+ medicines, "
                "with seamless SQLite fallback.",
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


@app.get("/")
async def root():
    """Root info endpoint."""
    return {
        "name": "Medicine Fuzzy Search API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "search_example": "/api/v1/search?q=augmentin%20625&limit=5",
        "database": db_manager.active_dialect,
        "is_fallback": db_manager.is_fallback,
    }


@app.get("/health")
async def health():
    """Basic health check with active database dialect info."""
    return {
        "status": "ok",
        "database": "connected",
        "dialect": db_manager.active_dialect,
        "is_fallback": db_manager.is_fallback,
    }
