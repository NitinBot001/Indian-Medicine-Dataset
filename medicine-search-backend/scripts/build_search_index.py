"""
Idempotent search index, lexicon, and search function builder (Sections 4 & 5).

Features:
- Ensures required PostgreSQL extensions (pg_trgm, fuzzystrmatch) are installed.
- Adds and populates search columns (name_clean, brand, form, name_ts, comp_ts, mfr_ts, all_ts).
- Builds optimized indexes (text_pattern_ops and GIN tsvectors).
- Rebuilds brand_lex and ingredient_lex tables.
- Drops obsolete/duplicate indexes to eliminate index bloat.
- Deploys/updates the search_medicines PL/pgSQL single round-trip engine function.
- Reports table and index sizes before and after.

Usage:
    python -m scripts.build_search_index
"""

import sys
import time
import psycopg
from app.core.config import get_settings


SEARCH_MEDICINES_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION search_medicines(
    p_tokens text[],
    p_lim int DEFAULT 10,
    p_fields text[] DEFAULT ARRAY['name', 'composition', 'manufacturer'],
    p_active_only bool DEFAULT false,
    p_min_price numeric DEFAULT NULL,
    p_max_price numeric DEFAULT NULL,
    p_mfr text DEFAULT NULL,
    p_form text DEFAULT NULL
)
RETURNS TABLE (
    medicine_id text,
    source_id varchar,
    name varchar,
    manufacturer_name varchar,
    composition text,
    pack_size_label varchar,
    price float8,
    is_discontinued boolean,
    match_tier int,
    matched_on text,
    match_reason text,
    score float8,
    corrected_query text,
    low_confidence boolean
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_cnt int := 0;
    v_q_phrase text;
    v_num_tokens int;
    v_has_name boolean;
    v_has_comp boolean;
    v_has_mfr boolean;
    v_t0 text;
    v_ts_and text;
    v_cand_brands text[];
    v_cand_salt text;
    v_tsquery tsquery;
BEGIN
    IF p_tokens IS NULL OR array_length(p_tokens, 1) = 0 THEN
        RETURN;
    END IF;

    v_q_phrase := array_to_string(p_tokens, ' ');
    v_num_tokens := array_length(p_tokens, 1);
    v_t0 := p_tokens[1];

    -- Fields restrictions
    v_has_name := 'name' = ANY(p_fields);
    v_has_comp := 'composition' = ANY(p_fields);
    v_has_mfr  := 'manufacturer' = ANY(p_fields);

    CREATE TEMP TABLE IF NOT EXISTS _search_tmp (
        med_id uuid PRIMARY KEY,
        m_source_id varchar,
        m_name varchar,
        m_mfr varchar,
        m_comp text,
        m_pack varchar,
        m_price float8,
        m_disc boolean,
        m_tier int,
        m_on text,
        m_reason text,
        m_score float8,
        m_corrected text,
        m_low_conf boolean,
        sort_order int
    ) ON COMMIT DROP;

    TRUNCATE _search_tmp;

    -- ============================================================
    -- TIER 0: Exact Name Match
    -- ============================================================
    IF v_has_name THEN
        INSERT INTO _search_tmp
        SELECT
            m.id, m.source_id, m.name, m.manufacturer_name,
            COALESCE(m.short_composition1, '') ||
                CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                     THEN ' + ' || m.short_composition2 ELSE '' END,
            m.pack_size_label, m.price::float8, m.is_discontinued,
            0, 'name', 'exact_name', 1.0::float8, NULL, false, 0
        FROM medicines m
        WHERE (m.name_clean = v_q_phrase OR m.name_normalized = v_q_phrase)
          AND (NOT p_active_only OR NOT m.is_discontinued)
          AND (p_min_price IS NULL OR m.price >= p_min_price)
          AND (p_max_price IS NULL OR m.price <= p_max_price)
          AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
          AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
        ORDER BY m.is_discontinued ASC, length(m.name), m.name
        LIMIT p_lim
        ON CONFLICT (med_id) DO NOTHING;

        SELECT count(*)::int INTO v_cnt FROM _search_tmp;
        IF v_cnt >= p_lim THEN
            RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
            RETURN;
        END IF;
    END IF;

    -- ============================================================
    -- TIER 1: Name STARTS WITH Whole Typed Phrase
    -- ============================================================
    IF v_has_name THEN
        INSERT INTO _search_tmp
        SELECT
            m.id, m.source_id, m.name, m.manufacturer_name,
            COALESCE(m.short_composition1, '') ||
                CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                     THEN ' + ' || m.short_composition2 ELSE '' END,
            m.pack_size_label, m.price::float8, m.is_discontinued,
            1, 'name', 'prefix',
            (0.95 + 0.04 * (length(v_q_phrase)::float8 / GREATEST(length(m.name_clean), 1)))::float8,
            NULL, false, 1
        FROM medicines m
        WHERE m.name_clean LIKE (v_q_phrase || '%')
          AND (NOT p_active_only OR NOT m.is_discontinued)
          AND (p_min_price IS NULL OR m.price >= p_min_price)
          AND (p_max_price IS NULL OR m.price <= p_max_price)
          AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
          AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
        ORDER BY m.is_discontinued ASC, length(m.name_clean), m.name
        LIMIT (p_lim - v_cnt)
        ON CONFLICT (med_id) DO NOTHING;

        SELECT count(*)::int INTO v_cnt FROM _search_tmp;
        IF v_cnt >= p_lim OR length(v_q_phrase) = 1 THEN
            RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
            RETURN;
        END IF;
    END IF;

    -- ============================================================
    -- TIER 3 / 4 (for Multi-token queries): Word-Prefix AND & Cross-field
    -- ============================================================
    IF v_num_tokens >= 2 AND v_cnt < p_lim THEN
        v_ts_and := array_to_string(p_tokens, ':* & ') || ':*';
        v_tsquery := to_tsquery('simple', v_ts_and);

        IF v_has_name THEN
            INSERT INTO _search_tmp
            SELECT
                m.id, m.source_id, m.name, m.manufacturer_name,
                COALESCE(m.short_composition1, '') ||
                    CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                         THEN ' + ' || m.short_composition2 ELSE '' END,
                m.pack_size_label, m.price::float8, m.is_discontinued,
                3, 'name', 'token_match', 0.78::float8, NULL, false, 3
            FROM medicines m
            WHERE m.name_ts @@ v_tsquery
              AND (NOT p_active_only OR NOT m.is_discontinued)
              AND (p_min_price IS NULL OR m.price >= p_min_price)
              AND (p_max_price IS NULL OR m.price <= p_max_price)
              AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
              AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
            ORDER BY m.is_discontinued ASC, length(m.name), m.name
            LIMIT (p_lim - v_cnt)
            ON CONFLICT (med_id) DO NOTHING;

            SELECT count(*)::int INTO v_cnt FROM _search_tmp;
            IF v_cnt >= p_lim THEN
                RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
                RETURN;
            END IF;
        END IF;

        IF v_cnt < p_lim AND (array_length(p_fields, 1) >= 2 OR (v_has_name AND (v_has_comp OR v_has_mfr))) THEN
            INSERT INTO _search_tmp
            SELECT
                m.id, m.source_id, m.name, m.manufacturer_name,
                COALESCE(m.short_composition1, '') ||
                    CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                         THEN ' + ' || m.short_composition2 ELSE '' END,
                m.pack_size_label, m.price::float8, m.is_discontinued,
                4, 'name', 'cross_field', 0.72::float8, NULL, false, 4
            FROM medicines m
            WHERE m.all_ts @@ v_tsquery
              AND (NOT p_active_only OR NOT m.is_discontinued)
              AND (p_min_price IS NULL OR m.price >= p_min_price)
              AND (p_max_price IS NULL OR m.price <= p_max_price)
              AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
              AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
            ORDER BY m.is_discontinued ASC, length(m.name), m.name
            LIMIT (p_lim - v_cnt)
            ON CONFLICT (med_id) DO NOTHING;

            SELECT count(*)::int INTO v_cnt FROM _search_tmp;
            IF v_cnt >= p_lim THEN
                RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
                RETURN;
            END IF;
        END IF;
    END IF;

    -- ============================================================
    -- TIER 2: Typo / Abbreviation on Brand
    -- ============================================================
    IF v_has_name AND v_cnt < p_lim THEN
        -- 2A. Typo against brand_lex (similarity >= 0.40 and levenshtein <= 2)
        IF length(v_t0) >= 4 THEN
            SELECT array_agg(b.word) INTO v_cand_brands
            FROM (
                SELECT word
                FROM brand_lex
                WHERE word % v_t0
                  AND similarity(word, v_t0) >= 0.40
                  AND levenshtein(word, v_t0) <= 2
                ORDER BY levenshtein(word, v_t0) ASC, cnt DESC
                LIMIT 6
            ) b;

            IF v_cand_brands IS NOT NULL AND array_length(v_cand_brands, 1) > 0 THEN
                INSERT INTO _search_tmp
                SELECT
                    m.id, m.source_id, m.name, m.manufacturer_name,
                    COALESCE(m.short_composition1, '') ||
                        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                             THEN ' + ' || m.short_composition2 ELSE '' END,
                    m.pack_size_label, m.price::float8, m.is_discontinued,
                    2, 'name', 'typo_match',
                    (0.85 - 0.05 * levenshtein(m.brand, v_t0))::float8,
                    m.brand::text, false, 2
                FROM medicines m
                WHERE m.brand = ANY(v_cand_brands)
                  AND (v_num_tokens = 1 OR m.name_clean LIKE (m.brand || ' %' || p_tokens[2] || '%'))
                  AND (NOT p_active_only OR NOT m.is_discontinued)
                  AND (p_min_price IS NULL OR m.price >= p_min_price)
                  AND (p_max_price IS NULL OR m.price <= p_max_price)
                  AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
                  AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
                ORDER BY array_position(v_cand_brands, m.brand), m.is_discontinued ASC, length(m.name)
                LIMIT (p_lim - v_cnt)
                ON CONFLICT (med_id) DO NOTHING;

                SELECT count(*)::int INTO v_cnt FROM _search_tmp;
                IF v_cnt >= p_lim THEN
                    RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
                    RETURN;
                END IF;
            END IF;
        END IF;

        -- 2B. Abbreviation candidates (length 2 to 4)
        IF v_cnt < p_lim AND length(v_t0) >= 2 AND length(v_t0) <= 4 AND v_t0 ~ '^[a-z]+$' THEN
            SELECT array_agg(b.word) INTO v_cand_brands
            FROM (
                SELECT word
                FROM brand_lex
                WHERE word LIKE (substring(v_t0 from 1 for 1) || '%')
                  AND word ~ ('^' || regexp_replace(v_t0, '(.)', '\1.*', 'g'))
                  AND word <> v_t0
                  AND length(word) <= length(v_t0) + 5
                ORDER BY cnt DESC, length(word) ASC
                LIMIT 6
            ) b;

            IF v_cand_brands IS NOT NULL AND array_length(v_cand_brands, 1) > 0 THEN
                INSERT INTO _search_tmp
                SELECT
                    m.id, m.source_id, m.name, m.manufacturer_name,
                    COALESCE(m.short_composition1, '') ||
                        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                             THEN ' + ' || m.short_composition2 ELSE '' END,
                    m.pack_size_label, m.price::float8, m.is_discontinued,
                    2, 'name', 'typo_match', 0.80::float8, NULL, false, 2
                FROM medicines m
                WHERE m.brand = ANY(v_cand_brands)
                  AND (v_num_tokens = 1 OR m.name_clean LIKE (m.brand || ' %' || p_tokens[2] || '%'))
                  AND (NOT p_active_only OR NOT m.is_discontinued)
                  AND (p_min_price IS NULL OR m.price >= p_min_price)
                  AND (p_max_price IS NULL OR m.price <= p_max_price)
                  AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
                  AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
                ORDER BY array_position(v_cand_brands, m.brand), m.is_discontinued ASC, length(m.name)
                LIMIT (p_lim - v_cnt)
                ON CONFLICT (med_id) DO NOTHING;

                SELECT count(*)::int INTO v_cnt FROM _search_tmp;
                IF v_cnt >= p_lim THEN
                    RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
                    RETURN;
                END IF;
            END IF;
        END IF;
    END IF;

    -- ============================================================
    -- TIER 5: Composition Match (Active Ingredient / Salt)
    -- ============================================================
    IF v_has_comp AND v_cnt < p_lim AND length(v_t0) >= 3 THEN
        SELECT word INTO v_cand_salt
        FROM ingredient_lex
        WHERE word = v_t0
        LIMIT 1;

        IF v_cand_salt IS NULL AND length(v_t0) >= 4 THEN
            SELECT word INTO v_cand_salt
            FROM ingredient_lex
            WHERE word % v_t0 AND similarity(word, v_t0) >= 0.40 AND levenshtein(word, v_t0) <= 2
            ORDER BY levenshtein(word, v_t0) ASC, df DESC
            LIMIT 1;
        END IF;

        IF v_cand_salt IS NOT NULL THEN
            v_tsquery := to_tsquery('simple', v_cand_salt || ':*');

            INSERT INTO _search_tmp
            SELECT
                m.id, m.source_id, m.name, m.manufacturer_name,
                COALESCE(m.short_composition1, '') ||
                    CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                         THEN ' + ' || m.short_composition2 ELSE '' END,
                m.pack_size_label, m.price::float8, m.is_discontinued,
                5, 'composition', 'composition_match',
                (CASE WHEN v_num_tokens > 1 AND m.short_composition1 LIKE ('%' || p_tokens[2] || '%')
                      THEN 0.68 ELSE 0.60 END)::float8,
                CASE WHEN v_cand_salt <> v_t0 THEN v_cand_salt::text ELSE NULL END,
                false, 5
            FROM medicines m
            WHERE m.comp_ts @@ v_tsquery
              AND (NOT p_active_only OR NOT m.is_discontinued)
              AND (p_min_price IS NULL OR m.price >= p_min_price)
              AND (p_max_price IS NULL OR m.price <= p_max_price)
              AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
              AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
            ORDER BY m.is_discontinued ASC,
                     CASE WHEN m.short_composition2 IS NULL OR m.short_composition2 = '' THEN 0 ELSE 1 END,
                     length(m.name), m.name
            LIMIT (p_lim - v_cnt)
            ON CONFLICT (med_id) DO NOTHING;

            SELECT count(*)::int INTO v_cnt FROM _search_tmp;
            IF v_cnt >= p_lim THEN
                RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
                RETURN;
            END IF;
        END IF;
    END IF;

    -- ============================================================
    -- TIER 6: Manufacturer Match
    -- ============================================================
    IF v_has_mfr AND v_cnt < p_lim AND length(v_t0) >= 3 THEN
        v_tsquery := to_tsquery('simple', v_t0 || ':*');

        INSERT INTO _search_tmp
        SELECT
            m.id, m.source_id, m.name, m.manufacturer_name,
            COALESCE(m.short_composition1, '') ||
                CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                     THEN ' + ' || m.short_composition2 ELSE '' END,
            m.pack_size_label, m.price::float8, m.is_discontinued,
            6, 'manufacturer', 'manufacturer_match', 0.50::float8, NULL, false, 6
        FROM medicines m
        WHERE (m.mfr_ts @@ v_tsquery OR m.manufacturer_normalized LIKE (v_q_phrase || '%'))
          AND (NOT p_active_only OR NOT m.is_discontinued)
          AND (p_min_price IS NULL OR m.price >= p_min_price)
          AND (p_max_price IS NULL OR m.price <= p_max_price)
          AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
          AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
        ORDER BY m.is_discontinued ASC, length(m.name), m.name
        LIMIT (p_lim - v_cnt)
        ON CONFLICT (med_id) DO NOTHING;

        SELECT count(*)::int INTO v_cnt FROM _search_tmp;
        IF v_cnt >= p_lim THEN
            RETURN QUERY SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf FROM _search_tmp t ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name) LIMIT p_lim;
            RETURN;
        END IF;
    END IF;

    -- ============================================================
    -- TIER 7: Last Resort Trigram Anywhere (flag low_confidence=true)
    -- ============================================================
    IF v_cnt < p_lim AND length(v_q_phrase) >= 4 THEN
        INSERT INTO _search_tmp
        SELECT
            m.id, m.source_id, m.name, m.manufacturer_name,
            COALESCE(m.short_composition1, '') ||
                CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
                     THEN ' + ' || m.short_composition2 ELSE '' END,
            m.pack_size_label, m.price::float8, m.is_discontinued,
            7, 'name', 'trigram_fallback',
            round((0.40 * similarity(m.name_normalized, v_q_phrase))::numeric, 4)::float8,
            NULL, true, 7
        FROM medicines m
        WHERE m.name_normalized % v_q_phrase
          AND similarity(m.name_normalized, v_q_phrase) >= 0.45
          AND (NOT p_active_only OR NOT m.is_discontinued)
          AND (p_min_price IS NULL OR m.price >= p_min_price)
          AND (p_max_price IS NULL OR m.price <= p_max_price)
          AND (p_mfr IS NULL OR m.manufacturer_normalized LIKE ('%' || p_mfr || '%') OR m.manufacturer_name ILIKE ('%' || p_mfr || '%'))
          AND (p_form IS NULL OR m.form = p_form OR m.name_clean LIKE ('%' || p_form || '%') OR m.pack_size_label ILIKE ('%' || p_form || '%'))
        ORDER BY m.is_discontinued ASC, similarity(m.name_normalized, v_q_phrase) DESC, length(m.name)
        LIMIT (p_lim - v_cnt)
        ON CONFLICT (med_id) DO NOTHING;
    END IF;

    RETURN QUERY
    SELECT t.med_id::text, t.m_source_id, t.m_name, t.m_mfr, t.m_comp, t.m_pack, t.m_price, t.m_disc, t.m_tier, t.m_on, t.m_reason, t.m_score, t.m_corrected, t.m_low_conf
    FROM _search_tmp t
    ORDER BY t.sort_order, t.m_disc, t.m_score DESC, length(t.m_name)
    LIMIT p_lim;

END;
$$;
"""


def get_relation_sizes(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                pg_size_pretty(pg_total_relation_size('medicines')) AS total_pretty,
                pg_size_pretty(pg_relation_size('medicines')) AS table_pretty,
                pg_size_pretty(pg_indexes_size('medicines')) AS indexes_pretty,
                pg_total_relation_size('medicines') AS total_bytes,
                pg_relation_size('medicines') AS table_bytes,
                pg_indexes_size('medicines') AS indexes_bytes;
        """)
        row = cur.fetchone()
        return {
            "total_pretty": row[0],
            "table_pretty": row[1],
            "indexes_pretty": row[2],
            "total_bytes": row[3],
            "table_bytes": row[4],
            "indexes_bytes": row[5],
        }


def build_search_indexes() -> None:
    settings = get_settings()
    db_url = settings.sync_database_url
    if not db_url or "sqlite" in db_url:
        print("build_search_index is designed for PostgreSQL. Skipping for SQLite.")
        return

    print("Connecting to PostgreSQL to build search indexes, lexicons, and search function...")
    t0 = time.perf_counter()

    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # 1. Measure initial size
            sizes_before = get_relation_sizes(conn)
            print(f"\n--- Initial Storage Sizes ---")
            print(f"  Table Data: {sizes_before['table_pretty']}")
            print(f"  Indexes:    {sizes_before['indexes_pretty']}")
            print(f"  Total:      {sizes_before['total_pretty']}\n")

            # 2. Extensions
            print("Step 1/7: Ensuring extensions (pg_trgm, fuzzystrmatch)...")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
            cur.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;")

            # 3. Add columns
            print("Step 2/7: Ensuring columns exist on medicines table...")
            cur.execute("""
            ALTER TABLE medicines
              ADD COLUMN IF NOT EXISTS name_clean text,
              ADD COLUMN IF NOT EXISTS brand varchar(255),
              ADD COLUMN IF NOT EXISTS form varchar(64),
              ADD COLUMN IF NOT EXISTS name_ts tsvector,
              ADD COLUMN IF NOT EXISTS comp_ts tsvector,
              ADD COLUMN IF NOT EXISTS mfr_ts tsvector,
              ADD COLUMN IF NOT EXISTS all_ts tsvector;
            """)

            # 4. Populate missing data
            print("Step 3/7: Checking / populating search columns...")
            cur.execute("SELECT COUNT(*) FROM medicines WHERE name_clean IS NULL OR all_ts IS NULL;")
            missing = cur.fetchone()[0]
            if missing > 0:
                print(f"  Populating search columns for {missing} rows...")
                cur.execute("""
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
            else:
                print("  All search columns are already populated.")

            # 5. Build indexes
            print("Step 4/7: Creating search indexes...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_medicines_name_clean ON medicines (name_clean text_pattern_ops);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_medicines_brand_discontinued ON medicines (brand, is_discontinued);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_medicines_name_ts ON medicines USING gin (name_ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_medicines_comp_ts ON medicines USING gin (comp_ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_medicines_all_ts ON medicines USING gin (all_ts);")

            # 6. Build Lexicons
            print("Step 5/7: Building brand_lex and ingredient_lex...")
            cur.execute("DROP TABLE IF EXISTS brand_lex;")
            cur.execute("""
            CREATE TABLE brand_lex AS
              SELECT brand AS word, count(*)::int AS cnt
              FROM medicines
              WHERE length(brand) >= 2
              GROUP BY brand;
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_brand_lex_pattern ON brand_lex (word text_pattern_ops);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_brand_lex_trgm ON brand_lex USING gin (word gin_trgm_ops);")

            cur.execute("DROP TABLE IF EXISTS ingredient_lex;")
            cur.execute("""
            CREATE TABLE ingredient_lex AS
              SELECT word, ndoc AS df
              FROM ts_stat('SELECT comp_ts FROM medicines')
              WHERE length(word) >= 3;
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ingredient_lex_pattern ON ingredient_lex (word text_pattern_ops);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ingredient_lex_trgm ON ingredient_lex USING gin (word gin_trgm_ops);")

            # 7. Drop bloat indexes
            print("Step 6/7: Dropping unused/duplicate bloat indexes...")
            cur.execute("DROP INDEX IF EXISTS idx_medicine_search_covering;")
            cur.execute("DROP INDEX IF EXISTS idx_medicine_name_trgm;")
            cur.execute("DROP INDEX IF EXISTS idx_medicine_name_normalized_btree;")
            cur.execute("DROP INDEX IF EXISTS idx_medicine_source_id;")

            # 8. Create/update search_medicines function
            print("Step 7/7: Deploying search_medicines PL/pgSQL function...")
            cur.execute(SEARCH_MEDICINES_FUNCTION_SQL)

            # 9. Analyze
            print("Updating planner statistics (ANALYZE)...")
            cur.execute("ANALYZE medicines;")
            cur.execute("ANALYZE brand_lex;")
            cur.execute("ANALYZE ingredient_lex;")

            sizes_after = get_relation_sizes(conn)
            elapsed = time.perf_counter() - t0

            # Count rows
            cur.execute("SELECT COUNT(*) FROM brand_lex;")
            brand_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ingredient_lex;")
            ingr_count = cur.fetchone()[0]

            print(f"\n{'='*65}")
            print("SEARCH INDEX AND FUNCTION BUILD COMPLETED")
            print(f"{'='*65}")
            print(f"  Time taken:      {elapsed:.1f}s")
            print(f"  Brand lexicon:   {brand_count:,} unique brands")
            print(f"  Ingredient lex:  {ingr_count:,} unique ingredient words")
            print(f"  Table Size:      {sizes_before['table_pretty']} -> {sizes_after['table_pretty']}")
            print(f"  Index Size:      {sizes_before['indexes_pretty']} -> {sizes_after['indexes_pretty']}")
            print(f"  Total Size:      {sizes_before['total_pretty']} -> {sizes_after['total_pretty']}")
            print(f"{'='*65}\n")


if __name__ == "__main__":
    build_search_indexes()
