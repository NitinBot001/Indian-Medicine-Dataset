"""
Database schema setup script.

Creates all tables and indexes directly using raw SQL.
This is used for initial setup; Alembic is for incremental migrations.

Usage:
    python -m scripts.setup_db
"""

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCHEMA_SQL = """
-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Catalog Sources
CREATE TABLE IF NOT EXISTS catalog_sources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    repository_url VARCHAR(512),
    license_attribution TEXT,
    update_policy TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Catalog Import Runs
CREATE TABLE IF NOT EXISTS catalog_import_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id UUID NOT NULL REFERENCES catalog_sources(id),
    source_commit VARCHAR(64),
    file_path VARCHAR(512),
    file_hash_sha256 VARCHAR(64),
    importer_version VARCHAR(32),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    total_rows INTEGER,
    accepted_rows INTEGER,
    rejected_rows INTEGER,
    error_report JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Catalog Versions
CREATE TABLE IF NOT EXISTS catalog_versions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version_label VARCHAR(128) UNIQUE NOT NULL,
    import_run_id UUID NOT NULL REFERENCES catalog_import_runs(id),
    published_by VARCHAR(255),
    published_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT false,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Medicines (main table)
CREATE TABLE IF NOT EXISTS medicines (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id VARCHAR(64) NOT NULL,
    name VARCHAR(512) NOT NULL,
    name_normalized VARCHAR(512) NOT NULL,
    price NUMERIC(10, 2),
    is_discontinued BOOLEAN DEFAULT false,
    manufacturer_name VARCHAR(512),
    manufacturer_normalized VARCHAR(512),
    type VARCHAR(64),
    pack_size_label VARCHAR(255),
    short_composition1 TEXT,
    short_composition2 TEXT,
    composition_normalized TEXT,
    salt_composition TEXT,
    catalog_version_id UUID REFERENCES catalog_versions(id),
    source_hash VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Medicine Ingredients
CREATE TABLE IF NOT EXISTS medicine_ingredients (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    medicine_id UUID NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
    ingredient_text TEXT NOT NULL,
    ingredient_normalized VARCHAR(512),
    strength_amount VARCHAR(64),
    strength_unit VARCHAR(32),
    source_field VARCHAR(32),
    sort_order INTEGER DEFAULT 0
);

-- Medicine Aliases
CREATE TABLE IF NOT EXISTS medicine_aliases (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    medicine_id UUID NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
    alias VARCHAR(512) NOT NULL,
    alias_normalized VARCHAR(512) NOT NULL,
    alias_type VARCHAR(32) DEFAULT 'source_name',
    provenance TEXT,
    review_status VARCHAR(32) DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- SEARCH PERFORMANCE INDEXES (critical for <100ms target)
-- ============================================================

-- B-tree indexes for exact lookups
CREATE INDEX IF NOT EXISTS idx_medicine_source_id ON medicines(source_id);
CREATE INDEX IF NOT EXISTS idx_medicine_name_normalized_btree ON medicines(name_normalized);
CREATE INDEX IF NOT EXISTS idx_medicine_discontinued ON medicines(is_discontinued);
CREATE INDEX IF NOT EXISTS idx_medicine_ingredient_medicine_id ON medicine_ingredients(medicine_id);
CREATE INDEX IF NOT EXISTS idx_medicine_alias_medicine_id ON medicine_aliases(medicine_id);

-- GIN trigram indexes for fuzzy search (the key to <100ms)
CREATE INDEX IF NOT EXISTS idx_medicine_name_trgm
    ON medicines USING gin (name_normalized gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_medicine_manufacturer_trgm
    ON medicines USING gin (manufacturer_normalized gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_medicine_composition_trgm
    ON medicines USING gin (composition_normalized gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_alias_normalized_trgm
    ON medicine_aliases USING gin (alias_normalized gin_trgm_ops);

-- B-tree prefix pattern indexes for sub-millisecond prefix & exact search
CREATE INDEX IF NOT EXISTS idx_medicine_name_prefix
    ON medicines (name_normalized varchar_pattern_ops);

CREATE INDEX IF NOT EXISTS idx_medicine_manufacturer_prefix
    ON medicines (manufacturer_normalized varchar_pattern_ops);

CREATE INDEX IF NOT EXISTS idx_medicine_composition_prefix
    ON medicines (composition_normalized varchar_pattern_ops);

-- GiST index for fast KNN typo ordering and token search
CREATE INDEX IF NOT EXISTS idx_medicine_name_gist
    ON medicines USING gist (name_normalized gist_trgm_ops);

-- Covering index for common search result fields to reduce heap lookups
CREATE INDEX IF NOT EXISTS idx_medicine_search_covering
    ON medicines(name_normalized)
    INCLUDE (id, source_id, name, manufacturer_name, short_composition1, short_composition2,
             pack_size_label, price, is_discontinued);

-- ============================================================
-- REGULATORY TABLES (Phase 2 — created now for schema completeness)
-- ============================================================

CREATE TABLE IF NOT EXISTS regulatory_sources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    authority VARCHAR(255) NOT NULL,
    page_url TEXT,
    source_type VARCHAR(64),
    publication_date DATE,
    retrieval_date DATE,
    content_hash VARCHAR(64),
    is_current BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS regulatory_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rule_reference VARCHAR(255),
    scope_type VARCHAR(32) NOT NULL DEFAULT 'product',
    rule_state VARCHAR(32) NOT NULL DEFAULT 'active',
    effective_from DATE,
    effective_to DATE,
    condition_text TEXT,
    source_evidence_id UUID REFERENCES regulatory_sources(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS medicine_regulatory_assessments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    medicine_id UUID NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
    assessment_state VARCHAR(32) NOT NULL DEFAULT 'unknown',
    applied_rule_ids UUID[],
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    rationale TEXT,
    previous_assessment_id UUID REFERENCES medicine_regulatory_assessments(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- AUDIT TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type VARCHAR(64) NOT NULL,
    entity_type VARCHAR(64),
    entity_id UUID,
    actor VARCHAR(255),
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Set pg_trgm similarity threshold lower for medicine name typos
-- This is a session-level setting; the search service sets it per query
-- ALTER SYSTEM SET pg_trgm.similarity_threshold = 0.2;

SELECT 'Schema setup complete. Tables and indexes created.' AS result;
"""


def setup_database(database_url: str | None = None) -> None:
    """Create all tables and indexes."""
    from app.core.config import normalize_db_url_for_sync

    raw_url = (
        database_url
        or os.getenv("AIVEN_DATABASE_URL")
        or os.getenv("DATABASE_URL_SYNC")
        or os.getenv("DATABASE_URL")
        or "postgresql://medicine:medicine_dev@localhost:5432/medicine_search"
    )
    db_url = normalize_db_url_for_sync(raw_url)

    # Print masked host info
    host_info = db_url.split("@")[1] if "@" in db_url else db_url
    print(f"Connecting to: {host_info}")

    conn = psycopg.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        print("✓ Database schema and indexes created successfully")
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Create database tables and indexes")
    parser.add_argument("--database-url", help="Connection URI (e.g. Aiven Service URI)")
    args = parser.parse_args()

    setup_database(args.database_url)
