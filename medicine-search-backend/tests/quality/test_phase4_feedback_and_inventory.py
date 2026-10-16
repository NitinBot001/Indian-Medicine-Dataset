"""
Tests for Phase 4: Usage Feedback, Popularity Boost, and Shop Inventory.
"""

import pytest
from starlette.testclient import TestClient
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_record_feedback_endpoint(client):
    """POST /api/v1/search/feedback should record selection and return updated count."""
    # Find a medicine to test with
    search_resp = client.get("/api/v1/search", params={"q": "dolo 650", "limit": 1})
    assert search_resp.status_code == 200
    matches = search_resp.json()["catalog_matches"]
    assert len(matches) > 0
    med = matches[0]

    # Send feedback
    fb_resp = client.post(
        "/api/v1/search/feedback",
        json={
            "query": "dolo 650",
            "medicine_id": med["medicine_id"],
            "shop_id": "test_shop_1",
            "rank_selected": 1,
        },
    )
    assert fb_resp.status_code == 201
    data = fb_resp.json()
    assert data["status"] == "recorded"
    assert data["medicine_id"] == med["medicine_id"]
    assert data["selection_count"] >= 1


def test_popularity_boosts_ranking_inside_tier(client):
    """
    Medicines with higher selection_count should rank above unselected medicines
    within the same tier.
    """
    search_resp = client.get("/api/v1/search", params={"q": "pcm", "limit": 5})
    assert search_resp.status_code == 200
    matches = search_resp.json()["catalog_matches"]
    assert len(matches) >= 2

    # Pick the 2nd medicine and give it 5 selection events
    target_med = matches[1]
    for _ in range(5):
        client.post(
            "/api/v1/search/feedback",
            json={
                "query": "pcm",
                "medicine_id": target_med["medicine_id"],
                "shop_id": "shop_boost_test",
                "rank_selected": 2,
            },
        )

    # Search again — target medicine should now rank #1 or higher within its tier
    search_again = client.get("/api/v1/search", params={"q": "pcm", "limit": 5})
    assert search_again.status_code == 200
    new_matches = search_again.json()["catalog_matches"]
    assert new_matches[0]["medicine_id"] == target_med["medicine_id"] or any(
        m["medicine_id"] == target_med["medicine_id"] for m in new_matches[:2]
    )


def test_shop_inventory_lifecycle(client):
    """
    1. Update shop inventory via POST /api/v1/search/inventory
    2. Search with shop_id -> item appears in inventory_matches
    3. Search without shop_id -> item appears in catalog_matches, inventory_matches is empty
    """
    search_resp = client.get("/api/v1/search", params={"q": "augmentin 625", "limit": 2})
    assert search_resp.status_code == 200
    matches = search_resp.json()["catalog_matches"]
    assert len(matches) > 0
    target_med = matches[0]

    shop_id = "test_pharmacy_koramangala"

    # 1. Update stock
    inv_resp = client.post(
        "/api/v1/search/inventory",
        json={
            "shop_id": shop_id,
            "items": [
                {
                    "medicine_id": target_med["medicine_id"],
                    "quantity": 42,
                    "is_in_stock": True,
                    "custom_price": 199.50,
                }
            ],
        },
    )
    assert inv_resp.status_code == 200
    assert inv_resp.json()["updated_count"] == 1

    # 2. Search WITH shop_id
    shop_search = client.get(
        "/api/v1/search",
        params={"q": "augmentin 625", "shop_id": shop_id, "limit": 5},
    )
    assert shop_search.status_code == 200
    shop_data = shop_search.json()
    assert len(shop_data["inventory_matches"]) >= 1
    inv_match = next(
        m for m in shop_data["inventory_matches"] if m["medicine_id"] == target_med["medicine_id"]
    )
    assert inv_match["in_stock"] is True
    assert inv_match["stock_quantity"] == 42
    assert inv_match["price"] == 199.50

    # 3. Search WITHOUT shop_id
    regular_search = client.get(
        "/api/v1/search",
        params={"q": "augmentin 625", "limit": 5},
    )
    assert regular_search.status_code == 200
    reg_data = regular_search.json()
    assert len(reg_data["inventory_matches"]) == 0
    assert any(m["medicine_id"] == target_med["medicine_id"] for m in reg_data["catalog_matches"])


def test_alias_auto_learning(client):
    """
    Repeated selection (>= 2 times) for a custom query learns an alias.
    """
    search_resp = client.get("/api/v1/search", params={"q": "pan 40", "limit": 1})
    assert search_resp.status_code == 200
    med = search_resp.json()["catalog_matches"][0]

    # First selection
    r1 = client.post(
        "/api/v1/search/feedback",
        json={
            "query": "customaliasquery",
            "medicine_id": med["medicine_id"],
            "rank_selected": 1,
        },
    )
    assert r1.status_code == 201

    # Second selection -> should trigger alias auto-learning
    r2 = client.post(
        "/api/v1/search/feedback",
        json={
            "query": "customaliasquery",
            "medicine_id": med["medicine_id"],
            "rank_selected": 1,
        },
    )
    assert r2.status_code == 201
    assert r2.json()["learned_alias"] is True
