"""
Search API route — the primary billing autocomplete endpoint.

GET /api/v1/search?q={query}&limit=10&fields=name,composition&active_only=true&shop_id={shop_id}
POST /api/v1/search/feedback
POST /api/v1/search/select
POST /api/v1/search/inventory

Returns medicines ranked across 8 tiers (T0-T7) with sub-10ms response times.
"""

import time

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.models import (
    MedicineMatch,
    SearchFeedbackRequest,
    SearchFeedbackResponse,
    SearchResponse,
    ShopInventoryBatchRequest,
    ShopInventoryResponse,
)
from app.services.search import (
    record_search_selection,
    search_medicines,
    update_shop_inventory_items,
)

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    fields: list[str] | None = Query(
        None,
        description="Comma-separated or repeated field names to match: name, composition, manufacturer",
    ),
    active_only: bool = Query(
        False,
        description="Exclude discontinued medicines from results",
    ),
    min_price: float | None = Query(
        None,
        ge=0.0,
        description="Minimum retail price filter",
    ),
    max_price: float | None = Query(
        None,
        ge=0.0,
        description="Maximum retail price filter",
    ),
    manufacturer: str | None = Query(
        None,
        description="Filter by manufacturer name",
    ),
    form: str | None = Query(
        None,
        description="Filter by dosage form (e.g. tablet, capsule, syrup, injection)",
    ),
    shop_id: str | None = Query(
        None,
        description="Optional shop identifier for local inventory matching (Phase 4)",
    ),
    debug: bool = Query(
        False,
        description="Include search execution profile and diagnostics in response",
    ),
    session: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """
    Fuzzy medicine search for billing autocomplete.

    Returns catalog matches ranked by relevance score across T0-T7 tiers with Phase 4
    popularity boost and optional shop inventory matches.
    """
    start = time.perf_counter()

    # Parse fields parameter (supports ?fields=name,composition and ?fields=name&fields=composition)
    parsed_fields: list[str] = []
    if fields:
        for f in fields:
            for part in f.split(","):
                part_clean = part.strip().lower()
                if part_clean in ("name", "composition", "manufacturer"):
                    if part_clean not in parsed_fields:
                        parsed_fields.append(part_clean)
    if not parsed_fields:
        parsed_fields = ["name", "composition", "manufacturer"]

    clean_mfr = manufacturer.strip().lower() if manufacturer and manufacturer.strip() else None
    clean_form = form.strip().lower() if form and form.strip() else None
    clean_shop = shop_id.strip() if shop_id and shop_id.strip() else None

    results = await search_medicines(
        session=session,
        query=q,
        limit=limit,
        fields=parsed_fields,
        active_only=active_only,
        min_price=min_price,
        max_price=max_price,
        manufacturer=clean_mfr,
        form=clean_form,
        shop_id=clean_shop,
    )

    latency_ms = (time.perf_counter() - start) * 1000

    all_matches = [
        MedicineMatch(
            medicine_id=r.medicine_id,
            source_id=r.source_id,
            name=r.name,
            manufacturer=r.manufacturer_name,
            composition=r.composition,
            pack_size=r.pack_size_label,
            price=r.custom_price if r.custom_price is not None else r.price,
            is_discontinued=r.is_discontinued,
            match_parameter=r.matched_on,
            match_reason=r.match_reason,
            score=r.score,
            selectable=not r.is_discontinued,
            match_tier=r.match_tier,
            matched_on=r.matched_on,
            corrected_query=r.corrected_query,
            low_confidence=r.low_confidence,
            in_stock=r.in_stock,
            stock_quantity=r.stock_quantity,
        )
        for r in results
    ]

    # Split into inventory vs catalog if shop_id is supplied
    inventory_matches: list[MedicineMatch] = []
    catalog_matches: list[MedicineMatch] = []

    if clean_shop:
        for m in all_matches:
            if m.in_stock is True and (m.stock_quantity or 0) > 0:
                inventory_matches.append(m)
            else:
                catalog_matches.append(m)
    else:
        catalog_matches = all_matches

    # Overall corrected query if any match corrected the token
    overall_corrected_query = next((r.corrected_query for r in results if r.corrected_query), None)

    debug_info = None
    if debug:
        debug_info = {
            "query": q,
            "fields": parsed_fields,
            "filters": {
                "active_only": active_only,
                "min_price": min_price,
                "max_price": max_price,
                "manufacturer": clean_mfr,
                "form": clean_form,
                "shop_id": clean_shop,
            },
            "tiers_matched": sorted(list({r.match_tier for r in results})),
            "total_matches": len(all_matches),
            "inventory_matches_count": len(inventory_matches),
            "catalog_matches_count": len(catalog_matches),
            "latency_ms": round(latency_ms, 3),
        }

    return SearchResponse(
        query=q,
        total_results=len(all_matches),
        latency_ms=round(latency_ms, 2),
        inventory_matches=inventory_matches,
        catalog_matches=catalog_matches,
        corrected_query=overall_corrected_query,
        debug_info=debug_info,
    )


@router.post("/feedback", response_model=SearchFeedbackResponse, status_code=status.HTTP_201_CREATED)
@router.post("/select", response_model=SearchFeedbackResponse, status_code=status.HTTP_201_CREATED)
async def record_feedback(
    payload: SearchFeedbackRequest,
    session: AsyncSession = Depends(get_db),
) -> SearchFeedbackResponse:
    """
    Record billing selection feedback (Phase 4).
    Increments selection_count popularity and auto-learns query aliases.
    """
    count, learned_alias = await record_search_selection(
        session=session,
        query=payload.query,
        medicine_id=payload.medicine_id,
        shop_id=payload.shop_id,
        rank_selected=payload.rank_selected,
    )

    return SearchFeedbackResponse(
        status="recorded",
        medicine_id=payload.medicine_id,
        query=payload.query,
        selection_count=count,
        learned_alias=learned_alias,
    )


@router.post("/inventory", response_model=ShopInventoryResponse)
async def update_inventory(
    payload: ShopInventoryBatchRequest,
    session: AsyncSession = Depends(get_db),
) -> ShopInventoryResponse:
    """
    Batch update or insert items in a shop's inventory (Phase 4).
    """
    count = await update_shop_inventory_items(
        session=session,
        shop_id=payload.shop_id,
        items=[it.model_dump() for it in payload.items],
    )

    return ShopInventoryResponse(
        status="ok",
        shop_id=payload.shop_id,
        updated_count=count,
    )
