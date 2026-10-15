"""
Search API route — the primary billing autocomplete endpoint.

GET /api/v1/search?q={query}&limit=10

Returns medicines ranked by relevance with latency tracking.
Target: p95 < 100ms for 253k medicines.
"""

import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.models import MedicineMatch, SearchResponse
from app.services.search import search_medicines

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    session: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """
    Fuzzy medicine search for billing autocomplete.

    Returns catalog matches ranked by relevance score.
    Inventory matches will be populated when shop context is available.
    """
    start = time.perf_counter()

    results = await search_medicines(session, q, limit=limit)

    latency_ms = (time.perf_counter() - start) * 1000

    catalog_matches = [
        MedicineMatch(
            medicine_id=r.medicine_id,
            source_id=r.source_id,
            name=r.name,
            manufacturer=r.manufacturer_name,
            composition=r.composition,
            pack_size=r.pack_size_label,
            price=r.price,
            is_discontinued=r.is_discontinued,
            match_parameter=getattr(r, "match_parameter", "name"),
            match_reason=r.match_reason,
            score=r.score,
            # Per plan: not selectable if discontinued; full eligibility gate in Phase 2
            selectable=not r.is_discontinued,
        )
        for r in results
    ]

    return SearchResponse(
        query=q,
        total_results=len(catalog_matches),
        latency_ms=round(latency_ms, 2),
        inventory_matches=[],  # Populated in Phase 4 with shop context
        catalog_matches=catalog_matches,
    )
