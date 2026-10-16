"""
Pydantic models for API request/response schemas.
"""

from pydantic import BaseModel, Field


class MedicineMatch(BaseModel):
    """A single medicine match result."""
    medicine_id: str
    source_id: str
    name: str
    manufacturer: str | None = None
    composition: str | None = None
    pack_size: str | None = None
    price: float | None = None
    is_discontinued: bool = False
    match_parameter: str = Field(
        default="name",
        description="Search parameter matched: 'name', 'composition', or 'manufacturer'"
    )
    match_reason: str = Field(
        description="Detailed match reason: exact_name, exact_alias, prefix, token_match, typo_match, composition_match, manufacturer_match"
    )
    score: float = Field(ge=0.0, le=1.0, description="Relevance score 0-1")
    selectable: bool = Field(
        default=True,
        description="Whether this item can be selected in billing (False if discontinued or not verified)"
    )
    match_tier: int = Field(default=1, description="Ranking ladder tier (T0-T7)")
    matched_on: str = Field(default="name", description="Field matched: name, composition, manufacturer")
    corrected_query: str | None = Field(default=None, description="Suggested correction token if typo or abbreviation")
    low_confidence: bool = Field(default=False, description="Flagged true if last resort match")
    in_stock: bool | None = Field(default=None, description="Whether available in shop inventory")
    stock_quantity: int | None = Field(default=None, description="Current stock quantity in shop")


class SearchResponse(BaseModel):
    """Response for the medicine search endpoint."""
    query: str
    total_results: int
    latency_ms: float = Field(description="Search latency in milliseconds")
    inventory_matches: list[MedicineMatch] = Field(
        default_factory=list,
        description="Matches from shop inventory (shown first)"
    )
    catalog_matches: list[MedicineMatch] = Field(
        default_factory=list,
        description="Matches from national catalog (shown second)"
    )
    corrected_query: str | None = Field(
        default=None,
        description="Suggested spelling or salt correction for the overall query"
    )
    debug_info: dict | None = Field(
        default=None,
        description="Search execution profile and diagnostics (returned when debug=true)"
    )


class SearchFeedbackRequest(BaseModel):
    """Event payload when a pharmacist selects a medicine for billing (Phase 4)."""
    query: str = Field(..., min_length=1, max_length=200, description="Search query string that was typed")
    medicine_id: str = Field(..., description="ID of the medicine selected")
    shop_id: str | None = Field(default=None, description="Optional shop identifier where billing occurred")
    rank_selected: int = Field(default=1, ge=1, le=100, description="1-based rank position of the item when clicked")


class SearchFeedbackResponse(BaseModel):
    """Response after recording user selection feedback."""
    status: str = "recorded"
    medicine_id: str
    query: str
    selection_count: int
    learned_alias: bool = False


class ShopInventoryItem(BaseModel):
    """Single item update for shop inventory."""
    medicine_id: str = Field(..., description="Medicine ID in catalog")
    quantity: int = Field(default=0, ge=0, description="Available stock quantity")
    is_in_stock: bool = Field(default=True, description="Whether currently in stock")
    custom_price: float | None = Field(default=None, ge=0.0, description="Optional shop-specific retail price")


class ShopInventoryBatchRequest(BaseModel):
    """Batch stock update request for a pharmacy shop."""
    shop_id: str = Field(..., min_length=1, max_length=128, description="Unique shop identifier")
    items: list[ShopInventoryItem] = Field(..., min_length=1, description="List of items to update")


class ShopInventoryResponse(BaseModel):
    """Response for inventory update."""
    status: str = "ok"
    shop_id: str
    updated_count: int


class ImportRequest(BaseModel):
    """Request to start a CSV/JSON import."""
    source_name: str = "indian_medicine_dataset"
    file_path: str | None = None
    source_commit: str | None = None


class ImportStatus(BaseModel):
    """Status of an import job."""
    import_id: str
    status: str
    total_rows: int | None = None
    accepted_rows: int | None = None
    rejected_rows: int | None = None
    error_report: dict | None = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    database: str = "connected"
    database_dialect: str = "postgresql"
    is_fallback: bool = False
    catalog_version: str | None = None
    medicine_count: int = 0
    row_count: int | None = None
    error: str | None = None
