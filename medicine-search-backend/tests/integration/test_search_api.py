"""
Integration tests for search API endpoints (plan.md Section 7 & 11).
"""

import os
import pytest
import httpx

BASE_URL = os.getenv("MEDICINE_SEARCH_BACKEND_URL", os.getenv("BASE_URL", "http://localhost:8000"))


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as c:
        yield c


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_catalog_status(client):
    resp = client.get("/api/v1/admin/catalog/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"
    assert data["medicine_count"] == 253937
    assert data["catalog_version"] is not None


def test_database_deduplication_integrity():
    """Verify that zero duplicates exist in the database and unique constraint is active."""
    import psycopg
    from app.core.config import get_settings, normalize_db_url_for_sync
    settings = get_settings()
    sync_url = normalize_db_url_for_sync(settings.DATABASE_URL)
    with psycopg.connect(sync_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), COUNT(DISTINCT product_identity_hash)
                FROM medicines;
            """)
            total_count, distinct_count = cur.fetchone()
            assert total_count == distinct_count, f"Duplicates detected: {total_count} total != {distinct_count} distinct"
            assert total_count == 253937


def test_exact_search_returns_fast(client):
    client.get("/api/v1/search", params={"q": "Augmentin 625 Duo Tablet", "limit": 5})
    resp = client.get("/api/v1/search", params={"q": "Augmentin 625 Duo Tablet", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["latency_ms"] < 100.0
    assert len(data["catalog_matches"]) >= 1
    top = data["catalog_matches"][0]
    assert top["name"] == "Augmentin 625 Duo Tablet"
    assert top["match_reason"] == "exact_name"
    assert top["score"] == 1.0
    assert top["selectable"] is True


def test_fuzzy_typo_search(client):
    # Typo: augmentn 625 instead of Augmentin 625
    client.get("/api/v1/search", params={"q": "augmentn 625", "limit": 5})
    resp = client.get("/api/v1/search", params={"q": "augmentn 625", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["latency_ms"] < 100.0
    assert len(data["catalog_matches"]) >= 1
    top = data["catalog_matches"][0]
    assert "Augmentin" in top["name"]
    assert top["match_reason"] == "typo_match"


def test_prefix_search(client):
    client.get("/api/v1/search", params={"q": "dolo 650", "limit": 5})
    resp = client.get("/api/v1/search", params={"q": "dolo 650", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["latency_ms"] < 100.0
    assert len(data["catalog_matches"]) >= 1
    top = data["catalog_matches"][0]
    assert "Dolo 650" in top["name"]


def test_discontinued_not_selectable(client):
    # Search for an item and verify selectable matches discontinued flag
    resp = client.get("/api/v1/search", params={"q": "augmentin", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    for item in data["catalog_matches"]:
        if item["is_discontinued"]:
            assert item["selectable"] is False
        else:
            assert item["selectable"] is True


def test_three_parameter_priority(client):
    """Verify that Name matches strictly outrank Composition matches."""
    resp = client.get("/api/v1/search", params={"q": "panto", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    # First match must be a name match
    assert matches[0]["match_parameter"] == "name"
    assert "panto" in matches[0]["name"].lower()
    
    # Check that any name match has a higher score than any composition match
    name_scores = [m["score"] for m in matches if m["match_parameter"] == "name"]
    comp_scores = [m["score"] for m in matches if m["match_parameter"] == "composition"]
    if name_scores and comp_scores:
        assert min(name_scores) > max(comp_scores)
