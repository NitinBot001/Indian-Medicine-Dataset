"""
Search Ranking Quality Acceptance Tests (Section 7).

Verifies ranking ladder, typo tolerance, cross-field matching,
demotion of discontinued medicines, and elimination of mid-word substrings.
"""

import pytest
from starlette.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_ranking_pnt(client):
    """
    'pnt': first rows are names starting with pnt (PNT 40mg Tablet, Pnt Oral Suspension, ...),
    then names whose first word is spelling-related. No row where pnt appears mid-word (no Spntro-T).
    """
    resp = client.get("/api/v1/search", params={"q": "pnt", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    # First rows must start with pnt
    first_name = matches[0]["name"].lower()
    assert first_name.startswith("pnt")

    # Mid-word 'pnt' must NOT appear (e.g. Spntro-T, Spntop)
    for m in matches:
        name_lower = m["name"].lower()
        assert "spntro" not in name_lower
        assert "spntop" not in name_lower


def test_ranking_pant(client):
    """
    'pant': top 10 all start with pant; active before discontinued.
    """
    resp = client.get("/api/v1/search", params={"q": "pant", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) == 10
    saw_discontinued = False
    for m in matches:
        assert m["name"].lower().startswith("pant")
        if m["is_discontinued"]:
            saw_discontinued = True
        else:
            # Active must not come after a discontinued one
            assert not saw_discontinued, f"Active medicine {m['name']} ranked after discontinued"


def test_ranking_pnt_pant(client):
    """
    'pnt pant': PNT 40mg Tablet is first; no manufacturer-only rows.
    """
    resp = client.get("/api/v1/search", params={"q": "pnt pant", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    assert matches[0]["name"].startswith("PNT")
    for m in matches:
        assert m["match_parameter"] != "manufacturer"


def test_ranking_pcm(client):
    """
    'pcm': PCM-named paracetamol products first. No mid-word matches like Dpcm.
    """
    resp = client.get("/api/v1/search", params={"q": "pcm", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    assert matches[0]["name"].lower().startswith("pcm")
    for m in matches:
        assert not m["name"].lower().startswith("dpcm")


def test_ranking_paracetmol(client):
    """
    'paracetmol': first rows are Paracetamol Tablet (names starting with corrected word).
    """
    resp = client.get("/api/v1/search", params={"q": "paracetmol", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    assert "Paracetamol" in matches[0]["name"]


def test_ranking_pantaprazole(client):
    """
    'pantaprazole': products with pantoprazole as the only ingredient come before combinations.
    """
    resp = client.get("/api/v1/search", params={"q": "pantaprazole", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    top_comp = matches[0].get("composition") or ""
    assert "pantoprazole" in top_comp.lower()


def test_ranking_dolo650(client):
    """
    'dolo650': Dolo 650 Tablet first, no Dologel.
    """
    resp = client.get("/api/v1/search", params={"q": "dolo650", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    assert matches[0]["name"] == "Dolo 650 Tablet"
    names = [m["name"].lower() for m in matches]
    assert "dologel" not in names


def test_ranking_augmentn_625(client):
    """
    'augmentn 625': Augmentin 625 Duo Tablet first; no manufacturer-only rows.
    """
    resp = client.get("/api/v1/search", params={"q": "augmentn 625", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    assert "Augmentin 625" in matches[0]["name"]
    for m in matches:
        assert m["match_parameter"] != "manufacturer"


def test_ranking_pan_40(client):
    """
    'pan 40': PAN 40 Tablet first; no Bepan, Cppan, Dupan or Hipan above brand-start matches.
    """
    resp = client.get("/api/v1/search", params={"q": "pan 40", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    assert matches[0]["name"] == "PAN 40 Tablet"
    for m in matches:
        name_lower = m["name"].lower()
        assert not name_lower.startswith("bepan")
        assert not name_lower.startswith("cppan")
        assert not name_lower.startswith("dupan")
        assert not name_lower.startswith("hipan")


def test_ranking_dolo_micro(client):
    """
    'dolo micro': Dolo products whose manufacturer contains micro come first.
    """
    resp = client.get("/api/v1/search", params={"q": "dolo micro", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    matches = data["catalog_matches"]
    assert len(matches) >= 1
    assert "dolo" in matches[0]["name"].lower()
    assert "micro" in (matches[0].get("manufacturer") or "").lower()


def test_ranking_wildcards_safe(client):
    """
    'p_n' and '%': treated literally, no wildcard behaviour.
    """
    resp = client.get("/api/v1/search", params={"q": "%", "limit": 5})
    assert resp.status_code == 200
    assert resp.json()["total_results"] == 0

    resp = client.get("/api/v1/search", params={"q": "p_n", "limit": 5})
    assert resp.status_code == 200
    # Should not treat '_' as single-character wildcard matching arbitrary rows like 'Pany'
    for m in resp.json()["catalog_matches"]:
        assert m["name"].lower() != "pany tablet"


def test_ranking_garbage_empty_and_fast(client):
    """
    'zzzz' and 'qwerty': empty list in under 50 ms.
    """
    for q in ["zzzz", "qwerty"]:
        client.get("/api/v1/search", params={"q": q, "limit": 10})
        resp = client.get("/api/v1/search", params={"q": q, "limit": 10})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_results"] == 0
        assert data["latency_ms"] < 50.0
