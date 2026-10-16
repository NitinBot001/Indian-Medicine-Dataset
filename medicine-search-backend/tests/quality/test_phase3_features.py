"""
Tests for Phase 3 Search Engine Features:
- Query filtering: fields, active_only, min_price, max_price, manufacturer, form
- Debug profiling: debug=true returns debug_info
- Corrected query propagation
- Loud fallback health check
"""

import os
import pytest
from starlette.testclient import TestClient
from app.main import app
from app.core.config import get_settings
from app.db.session import db_manager


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_search_filters_active_only(client):
    """active_only=true must return zero discontinued medicines."""
    resp = client.get("/api/v1/search", params={"q": "tablet", "limit": 20, "active_only": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_results"] > 0
    for match in data["catalog_matches"]:
        assert match["is_discontinued"] is False
        assert match["selectable"] is True


def test_search_filters_price_range(client):
    """min_price and max_price must filter catalog matches appropriately."""
    resp = client.get(
        "/api/v1/search",
        params={"q": "dolo", "limit": 10, "min_price": 20.0, "max_price": 50.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_results"] > 0
    for match in data["catalog_matches"]:
        if match["price"] is not None:
            assert match["price"] >= 20.0
            assert match["price"] <= 50.0


def test_search_filters_manufacturer(client):
    """manufacturer filter should only return items from matching company."""
    resp = client.get(
        "/api/v1/search",
        params={"q": "dolo", "limit": 10, "manufacturer": "micro labs"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_results"] > 0
    for match in data["catalog_matches"]:
        assert match["manufacturer"] is not None
        assert "micro labs" in match["manufacturer"].lower()


def test_search_filters_form(client):
    """form filter should restrict results to the specified dosage form."""
    resp = client.get(
        "/api/v1/search",
        params={"q": "dolo", "limit": 10, "form": "tablet"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_results"] > 0
    for match in data["catalog_matches"]:
        assert match["pack_size"] is not None
        assert "tablet" in match["pack_size"].lower()


def test_search_fields_restriction_composition(client):
    """fields=composition should only search composition and not name."""
    resp = client.get(
        "/api/v1/search",
        params={"q": "paracetamol", "limit": 10, "fields": "composition"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_results"] > 0
    for match in data["catalog_matches"]:
        assert match["matched_on"] == "composition"
        assert match["composition"] is not None
        assert "paracetamol" in match["composition"].lower()


def test_search_debug_flag(client):
    """debug=true should return diagnostics profiling info."""
    resp = client.get("/api/v1/search", params={"q": "augmentin 625", "limit": 5, "debug": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["debug_info"] is not None
    debug = data["debug_info"]
    assert "tiers_matched" in debug
    assert "latency_ms" in debug
    assert "filters" in debug


def test_search_corrected_query(client):
    """Typo in salt query pantaprazole should propagate corrected_query."""
    resp = client.get("/api/v1/search", params={"q": "pantaprazole", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["corrected_query"] in ("pantoprazole", "panprazole")
