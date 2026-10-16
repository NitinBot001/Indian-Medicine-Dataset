"""Search schema, tsvectors, lexicons, and index optimization (Phase 2).

Revision ID: 20261016_0001
Revises: 
Create Date: 2026-10-16 03:20:00.000000
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "20261016_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;")

    # 2. Add columns to medicines
    op.execute("""
    ALTER TABLE medicines
      ADD COLUMN IF NOT EXISTS name_clean text,
      ADD COLUMN IF NOT EXISTS brand varchar(255),
      ADD COLUMN IF NOT EXISTS form varchar(64),
      ADD COLUMN IF NOT EXISTS name_ts tsvector,
      ADD COLUMN IF NOT EXISTS comp_ts tsvector,
      ADD COLUMN IF NOT EXISTS mfr_ts tsvector,
      ADD COLUMN IF NOT EXISTS all_ts tsvector;
    """)

    # 3. Populate new columns
    op.execute("""
    UPDATE medicines SET
      name_clean = btrim(regexp_replace(name_normalized, '[^a-z0-9]+', ' ', 'g')),
      name_ts = to_tsvector('simple', regexp_replace(name_normalized, '[^a-z0-9]+', ' ', 'g')),
      comp_ts = to_tsvector('simple', regexp_replace(coalesce(composition_normalized,''), '[^a-z0-9]+', ' ', 'g')),
      mfr_ts  = to_tsvector('simple', regexp_replace(coalesce(manufacturer_normalized,''), '[^a-z0-9]+', ' ', 'g')),
      form = split_part(btrim(lower(coalesce(pack_size_label, ''))), ' ', 1);

    UPDATE medicines SET
      brand = split_part(name_clean, ' ', 1),
      all_ts = name_ts || comp_ts || mfr_ts;
    """)

    # 4. Create new optimized indexes
    op.execute("CREATE INDEX IF NOT EXISTS idx_medicines_name_clean ON medicines (name_clean text_pattern_ops);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_medicines_brand_discontinued ON medicines (brand, is_discontinued);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_medicines_name_ts ON medicines USING gin (name_ts);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_medicines_comp_ts ON medicines USING gin (comp_ts);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_medicines_all_ts ON medicines USING gin (all_ts);")

    # 5. Build brand lexicon
    op.execute("DROP TABLE IF EXISTS brand_lex;")
    op.execute("""
    CREATE TABLE brand_lex AS
      SELECT brand AS word, count(*)::int AS cnt
      FROM medicines
      WHERE length(brand) >= 2
      GROUP BY brand;
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_brand_lex_pattern ON brand_lex (word text_pattern_ops);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brand_lex_trgm ON brand_lex USING gin (word gin_trgm_ops);")

    # 6. Build ingredient lexicon
    op.execute("DROP TABLE IF EXISTS ingredient_lex;")
    op.execute("""
    CREATE TABLE ingredient_lex AS
      SELECT word, ndoc AS df
      FROM ts_stat('SELECT comp_ts FROM medicines')
      WHERE length(word) >= 3;
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ingredient_lex_pattern ON ingredient_lex (word text_pattern_ops);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ingredient_lex_trgm ON ingredient_lex USING gin (word gin_trgm_ops);")

    # 7. Drop unused / duplicate bloat indexes
    op.execute("DROP INDEX IF EXISTS idx_medicine_search_covering;")
    op.execute("DROP INDEX IF EXISTS idx_medicine_name_trgm;")
    op.execute("DROP INDEX IF EXISTS idx_medicine_name_normalized_btree;")
    op.execute("DROP INDEX IF EXISTS idx_medicine_source_id;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ingredient_lex;")
    op.execute("DROP TABLE IF EXISTS brand_lex;")
    op.execute("DROP INDEX IF EXISTS idx_medicines_all_ts;")
    op.execute("DROP INDEX IF EXISTS idx_medicines_comp_ts;")
    op.execute("DROP INDEX IF EXISTS idx_medicines_name_ts;")
    op.execute("DROP INDEX IF EXISTS idx_medicines_brand_discontinued;")
    op.execute("DROP INDEX IF EXISTS idx_medicines_name_clean;")
    op.execute("""
    ALTER TABLE medicines
      DROP COLUMN IF EXISTS all_ts,
      DROP COLUMN IF EXISTS mfr_ts,
      DROP COLUMN IF EXISTS comp_ts,
      DROP COLUMN IF EXISTS name_ts,
      DROP COLUMN IF EXISTS form,
      DROP COLUMN IF EXISTS brand,
      DROP COLUMN IF EXISTS name_clean;
    """)
