"""
Unit tests for medicine deduplication and identity hashing.
"""

from app.importers.csv_importer import compute_product_identity_hash
from app.services.normalization import (
    normalize_medicine_name,
    normalize_manufacturer,
    normalize_composition,
)


def test_product_identity_hash_deterministic():
    """Verify that same inputs always produce identical hash."""
    h1 = compute_product_identity_hash(
        "augmentin 625 duo tablet",
        "glaxo smithkline",
        "strip of 10 tablets",
        "amoxycillin (500mg) + clavulanic acid (125mg)",
    )
    h2 = compute_product_identity_hash(
        "augmentin 625 duo tablet",
        "glaxo smithkline",
        "strip of 10 tablets",
        "amoxycillin (500mg) + clavulanic acid (125mg)",
    )
    assert h1 == h2
    assert len(h1) == 32


def test_product_identity_hash_case_and_whitespace_invariant():
    """Verify that casing differences produce identical hash after normalization."""
    name1 = normalize_medicine_name("  Dolo 650 Tablet  ")
    name2 = normalize_medicine_name("dolo 650 tablet")
    mfr1 = normalize_manufacturer("Micro Labs Ltd")
    mfr2 = normalize_manufacturer("micro labs ltd")
    pack = "strip of 15 tablets"
    comp = normalize_composition("paracetamol (650mg)", None)

    h1 = compute_product_identity_hash(name1, mfr1, pack, comp)
    h2 = compute_product_identity_hash(name2, mfr2, pack, comp)

    assert h1 == h2


def test_product_identity_hash_detects_duplicates():
    """
    Verify that identical medicine attributes from duplicate CSV entries
    resolve to the exact same hash.
    """
    # Sample from dataset duplicate
    name1 = normalize_medicine_name("Curemite Soap")
    mfr1 = normalize_manufacturer("Kivi")
    pack1 = "packet of 75 gm Soap"
    comp1 = normalize_composition("Permethrin (NA)", None)

    name2 = normalize_medicine_name("Curemite Soap")
    mfr2 = normalize_manufacturer("Kivi")
    pack2 = "packet of 75 gm Soap"
    comp2 = normalize_composition("Permethrin (NA)", None)

    hash1 = compute_product_identity_hash(name1, mfr1, pack1, comp1)
    hash2 = compute_product_identity_hash(name2, mfr2, pack2, comp2)

    assert hash1 == hash2
    assert hash1 == "80865e7b366680e6556a58e18b742e4e"


def test_different_medicines_produce_different_hashes():
    """Verify that different medicines have distinct hashes."""
    h1 = compute_product_identity_hash(
        "augmentin 625 duo tablet",
        "glaxo smithkline",
        "strip of 10 tablets",
        "amoxycillin + clavulanic acid",
    )
    h2 = compute_product_identity_hash(
        "augmentin 375 tablet",
        "glaxo smithkline",
        "strip of 10 tablets",
        "amoxycillin + clavulanic acid",
    )
    assert h1 != h2


def test_database_blocks_duplicate_insert():
    """Verify that PostgreSQL unique constraint raises error on duplicate insert."""
    import pytest
    import psycopg
    from psycopg.errors import UniqueViolation
    from app.core.config import get_settings, normalize_db_url_for_sync

    sync_url = normalize_db_url_for_sync(get_settings().DATABASE_URL)
    with psycopg.connect(sync_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT product_identity_hash FROM medicines LIMIT 1;")
            existing_hash = cur.fetchone()[0]

            with pytest.raises(UniqueViolation):
                cur.execute("""
                    INSERT INTO medicines (id, source_id, name, name_normalized, product_identity_hash)
                    VALUES (gen_random_uuid(), 'test_dup', 'Duplicate Test', 'duplicate test', %s);
                """, (existing_hash,))
        conn.rollback()

