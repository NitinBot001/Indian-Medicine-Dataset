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
    match_reason: str = Field(
        description="Why this result matched: exact_name, exact_alias, prefix, typo_match, composition_match"
    )
    score: float = Field(ge=0.0, le=1.0, description="Relevance score 0-1")
    selectable: bool = Field(
        default=True,
        description="Whether this item can be selected in billing (False if discontinued or not verified)"
    )


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
    catalog_version: str | None = None
    medicine_count: int = 0
