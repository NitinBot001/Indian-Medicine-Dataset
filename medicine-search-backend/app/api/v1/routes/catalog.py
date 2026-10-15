"""
Catalog import API routes.

POST /api/v1/admin/catalog/import — trigger CSV import
GET  /api/v1/admin/catalog/status  — check import status and catalog info
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.models import HealthResponse

router = APIRouter(prefix="/admin/catalog", tags=["catalog"])


@router.get("/status", response_model=HealthResponse)
async def catalog_status(
    session: AsyncSession = Depends(get_db),
) -> HealthResponse:
    """Get catalog status including medicine count."""
    try:
        result = await session.execute(text("SELECT count(*) FROM medicines"))
        count = result.scalar_one()

        version_result = await session.execute(
            text("SELECT version_label FROM catalog_versions WHERE is_active = true LIMIT 1")
        )
        version = version_result.scalar_one_or_none()

        return HealthResponse(
            status="ok",
            database="connected",
            catalog_version=version,
            medicine_count=count,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
