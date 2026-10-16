import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import get_settings
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

# CORS — standard API security configuration (wildcard origins with credentials=False)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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
async def health(response: Response):
    """
    Health check endpoint with loud fallback degradation detection.
    Returns 503 Service Unavailable if degraded to SQLite fallback with <= 21 rows
    unless ALLOW_TINY_FALLBACK=true.
    """
    settings = get_settings()
    is_fallback = db_manager.is_fallback
    row_count = None

    if is_fallback:
        try:
            async with db_manager.session() as session:
                res = await session.execute(text("SELECT count(*) FROM medicines"))
                row_count = res.scalar() or 0
        except Exception as e:
            logger.error(f"Error checking medicine count during health check: {e}")
            row_count = 0

        if row_count <= 21 and not settings.ALLOW_TINY_FALLBACK:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "degraded",
                "database": "fallback_sqlite",
                "dialect": db_manager.active_dialect,
                "is_fallback": True,
                "row_count": row_count,
                "error": "PostgreSQL is unreachable and catalogue is running on tiny SQLite fallback (<= 21 rows). Set ALLOW_TINY_FALLBACK=true to allow."
            }

    return {
        "status": "ok",
        "database": "connected",
        "dialect": db_manager.active_dialect,
        "is_fallback": is_fallback,
        "row_count": row_count,
    }
