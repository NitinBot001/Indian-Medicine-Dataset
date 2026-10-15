"""
Fuzzy search service for medicines using PostgreSQL pg_trgm and SQLite fallback.

Strict 3-Parameter Search Hierarchy (User Requirement):
1. PARAMETER 1: Medicine Name (नाम) — Highest Priority (Top of List)
   Results within this group are sorted by name-similarity to the query (closest name first):
   - Exact Name Match  → score 1.00
   - Prefix Name Match → scored by closeness of name to query
   - Exact/Prefix Approved Alias → score 0.90
   - Word/Token Name Match → scored by closeness
   - Typo / Trigram Fuzzy Name Match → scored by similarity
   * If Name matches fulfill limit, return immediately (zero composition/company pollution).

2. PARAMETER 2: Composition (कंपोजिशन) — Second Priority (Middle of List)
   * Only executed if Name matches < limit.
   Results within this group are also sorted by name-similarity to query:
   - Prefix / Word Match on active salts/composition
   - Substring / Fuzzy Match on active salts/composition

3. PARAMETER 3: Company / Manufacturer (कंपनी) — Third Priority (Bottom of List)
   * Only executed if Name + Composition matches < limit.
   Results within this group are sorted by name-similarity to query:
   - Prefix / Word Match on company name
   - Substring / Fuzzy Match on company name

Final output = Name_bucket + Composition_bucket + Manufacturer_bucket
(strictly NO cross-bucket mixing — each bucket sorted by name-similarity internally)
"""

import difflib
import re
import time
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.normalization import normalize_medicine_name


class MatchReason(str, Enum):
    EXACT_NAME = "exact_name"
    EXACT_ALIAS = "exact_alias"
    PREFIX = "prefix"
    TOKEN_MATCH = "token_match"
    TYPO_MATCH = "typo_match"
    COMPOSITION_MATCH = "composition_match"
    MANUFACTURER_MATCH = "manufacturer_match"


@dataclass
class SearchResult:
    medicine_id: str
    source_id: str
    name: str
    manufacturer_name: str | None
    composition: str | None
    pack_size_label: str | None
    price: float | None
    is_discontinued: bool
    match_parameter: str  # "name", "composition", or "manufacturer"
    match_reason: str     # "exact_name", "prefix", "token_match", "typo_match", etc.
    score: float


# In-memory LRU query cache (5 min TTL) for ultra-fast autocomplete
_CACHE: dict[str, tuple[float, list[SearchResult]]] = {}
_CACHE_TTL_SECONDS = 300
_MAX_CACHE_SIZE = 1000


# ============================================================
# PARAMETER 1: MEDICINE NAME POSTGRESQL QUERIES (Score 0.75 - 1.00)
# ============================================================

# 1A. Exact Name Match (B-Tree: <0.5ms)
PG_EXACT_NAME_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'name' AS match_parameter,
    'exact_name' AS match_reason,
    1.0::float AS score
FROM medicines m
WHERE m.name_normalized = :q
LIMIT :limit
""")

# 1B. Prefix Name Match (name starts with query)
PG_PREFIX_NAME_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'name' AS match_parameter,
    'prefix' AS match_reason,
    (0.91 + 0.06 * (length(:q)::float / GREATEST(length(m.name_normalized), 1)))::float AS score
FROM medicines m
WHERE m.name_normalized LIKE (:q || '%')
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

# 1C. Exact & Prefix Alias Matches
PG_ALIAS_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'name' AS match_parameter,
    'exact_alias' AS match_reason,
    0.90::float AS score
FROM medicine_aliases ma
JOIN medicines m ON m.id = ma.medicine_id
WHERE (ma.alias_normalized = :q OR ma.alias_normalized LIKE (:q || '%'))
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
LIMIT :remaining
""")

# 1D. Token / Word Match in Medicine Name
PG_TOKEN_NAME_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'name' AS match_parameter,
    'token_match' AS match_reason,
    (0.85 + 0.04 * (length(:q)::float / GREATEST(length(m.name_normalized), 1)))::float AS score
FROM medicines m
WHERE (m.name_normalized LIKE ('% ' || :q || '%') OR m.name_normalized LIKE ('%' || :q || '%'))
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

# 1E. Decomposed Fuzzy for Brand Typo + Number (e.g. "pantoseb 40" -> "pantosec 40")
PG_FUZZY_DECOMPOSED_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'name' AS match_parameter,
    'typo_match' AS match_reason,
    (0.75 + 0.09 * similarity(m.name_normalized, :q))::float AS score
FROM medicines m
WHERE m.name_normalized % :brand_q
  AND m.name_normalized LIKE ('%' || :num_q || '%')
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY similarity(m.name_normalized, :q) DESC
LIMIT :remaining
""")

# 1F. Standard Trigram Fuzzy Name Typo Match (e.g. "pantoseb" -> "pantosec")
PG_FUZZY_NAME_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'name' AS match_parameter,
    'typo_match' AS match_reason,
    (0.75 + 0.09 * similarity(m.name_normalized, :q))::float AS score
FROM medicines m
WHERE m.name_normalized % :q
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY similarity(m.name_normalized, :q) DESC
LIMIT :remaining
""")


# ============================================================
# PARAMETER 2: COMPOSITION POSTGRESQL QUERIES (Score 0.55 - 0.70)
# ============================================================

# 2A. Prefix / Word Match on Composition (Active Ingredients / Salts)
PG_PREFIX_COMPOSITION_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'composition' AS match_parameter,
    'composition_match' AS match_reason,
    (0.65 + 0.05 * (length(:q)::float / GREATEST(length(COALESCE(m.composition_normalized, '')), 1)))::float AS score
FROM medicines m
WHERE (m.composition_normalized LIKE (:q || '%') OR m.composition_normalized LIKE ('% ' || :q || '%'))
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

# 2B. Substring / Fuzzy Trigram Match on Composition
PG_FUZZY_COMPOSITION_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'composition' AS match_parameter,
    'composition_match' AS match_reason,
    (0.55 + 0.09 * similarity(COALESCE(m.composition_normalized, ''), :q))::float AS score
FROM medicines m
WHERE (m.composition_normalized % :q OR m.composition_normalized LIKE ('%' || :q || '%'))
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY similarity(COALESCE(m.composition_normalized, ''), :q) DESC, length(m.name_normalized)
LIMIT :remaining
""")


# ============================================================
# PARAMETER 3: COMPANY / MANUFACTURER POSTGRESQL QUERIES (Score 0.35 - 0.50)
# ============================================================

# 3A. Prefix / Word Match on Company Name
PG_PREFIX_MANUFACTURER_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'manufacturer' AS match_parameter,
    'manufacturer_match' AS match_reason,
    (0.45 + 0.05 * (length(:q)::float / GREATEST(length(COALESCE(m.manufacturer_normalized, '')), 1)))::float AS score
FROM medicines m
WHERE (m.manufacturer_normalized LIKE (:q || '%') OR m.manufacturer_normalized LIKE ('% ' || :q || '%'))
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

# 3B. Substring / Fuzzy Trigram Match on Company Name
PG_FUZZY_MANUFACTURER_QUERY = text("""
SELECT
    m.id::text AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    m.price::float,
    m.is_discontinued,
    'manufacturer' AS match_parameter,
    'manufacturer_match' AS match_reason,
    (0.35 + 0.09 * similarity(COALESCE(m.manufacturer_normalized, ''), :q))::float AS score
FROM medicines m
WHERE (m.manufacturer_normalized % :q OR m.manufacturer_normalized LIKE ('%' || :q || '%'))
  AND (CAST(:existing_ids AS text[]) IS NULL OR NOT (m.id::text = ANY(CAST(:existing_ids AS text[]))))
ORDER BY similarity(COALESCE(m.manufacturer_normalized, ''), :q) DESC
LIMIT :remaining
""")


# ============================================================
# SQLITE FALLBACK QUERIES (3 STRICT PARAMETERS)
# ============================================================

SQLITE_EXACT_NAME_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    'name' AS match_parameter,
    'exact_name' AS match_reason,
    1.0 AS score
FROM medicines m
WHERE m.name_normalized = :q
LIMIT :limit
""")

SQLITE_PREFIX_NAME_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    'name' AS match_parameter,
    'prefix' AS match_reason,
    (0.91 + 0.06 * (CAST(length(:q) AS REAL) / CASE WHEN length(m.name_normalized) > 1 THEN length(m.name_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE m.name_normalized LIKE (:q || '%')
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

SQLITE_ALIAS_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    'name' AS match_parameter,
    'exact_alias' AS match_reason,
    0.90 AS score
FROM medicine_aliases ma
JOIN medicines m ON m.id = ma.medicine_id
WHERE ma.alias_normalized = :q OR ma.alias_normalized LIKE (:q || '%')
LIMIT :remaining
""")

SQLITE_TOKEN_NAME_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    'name' AS match_parameter,
    'token_match' AS match_reason,
    (0.85 + 0.04 * (CAST(length(:q) AS REAL) / CASE WHEN length(m.name_normalized) > 1 THEN length(m.name_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE m.name_normalized LIKE ('% ' || :q || '%') OR m.name_normalized LIKE ('%' || :q || '%')
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

SQLITE_PREFIX_COMPOSITION_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    'composition' AS match_parameter,
    'composition_match' AS match_reason,
    (0.65 + 0.05 * (CAST(length(:q) AS REAL) / CASE WHEN length(COALESCE(m.composition_normalized, '')) > 1 THEN length(m.composition_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE m.composition_normalized LIKE (:q || '%') OR m.composition_normalized LIKE ('% ' || :q || '%')
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

SQLITE_PREFIX_MANUFACTURER_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued,
    'manufacturer' AS match_parameter,
    'manufacturer_match' AS match_reason,
    (0.45 + 0.05 * (CAST(length(:q) AS REAL) / CASE WHEN length(COALESCE(m.manufacturer_normalized, '')) > 1 THEN length(m.manufacturer_normalized) ELSE 1 END)) AS score
FROM medicines m
WHERE m.manufacturer_normalized LIKE (:q || '%') OR m.manufacturer_normalized LIKE ('% ' || :q || '%')
ORDER BY length(m.name_normalized), m.name_normalized
LIMIT :remaining
""")

SQLITE_CANDIDATES_QUERY = text("""
SELECT
    CAST(m.id AS TEXT) AS medicine_id,
    m.source_id,
    m.name,
    m.name_normalized,
    m.manufacturer_name,
    COALESCE(m.short_composition1, '') ||
        CASE WHEN m.short_composition2 IS NOT NULL AND m.short_composition2 != ''
            THEN ' + ' || m.short_composition2
            ELSE ''
        END AS composition,
    m.pack_size_label,
    CAST(m.price AS REAL) AS price,
    m.is_discontinued
FROM medicines m
WHERE (m.name_normalized LIKE (:first_char || '%'))
   OR (:num != '' AND m.name_normalized LIKE ('%' || :num || '%'))
   OR (:word != '' AND m.name_normalized LIKE ('%' || :word || '%'))
LIMIT 300
""")


# ============================================================
# POSTGRESQL IMPLEMENTATION
# ============================================================

async def search_medicines_postgres(
    session: AsyncSession,
    q_norm: str,
    limit: int = 10,
    *,
    similarity_threshold: float = 0.25,
) -> list[SearchResult]:
    """
    Search medicines on PostgreSQL strictly adhering to the 3-parameter priority:
    1. Medicine Name (Top priority: Exact, Prefix, Alias, Token, Typo)
    2. Composition (Middle priority: Prefix, Substring, Fuzzy)
    3. Company / Manufacturer (Bottom priority: Prefix, Substring, Fuzzy)

    Each parameter's results are collected into a separate bucket, sorted internally
    by name-similarity to the query (closest match first), then concatenated strictly:
    name_bucket → composition_bucket → manufacturer_bucket.
    """
    seen_ids: set[str] = set()
    name_bucket: list[SearchResult] = []
    comp_bucket: list[SearchResult] = []
    mfr_bucket: list[SearchResult] = []

    def make_and_add(row, bucket: list, match_param: str):
        """Create a SearchResult and add to the given bucket if not already seen."""
        if row.medicine_id in seen_ids:
            return
        seen_ids.add(row.medicine_id)
        bucket.append(
            SearchResult(
                medicine_id=row.medicine_id,
                source_id=row.source_id,
                name=row.name,
                manufacturer_name=row.manufacturer_name,
                composition=row.composition,
                pack_size_label=row.pack_size_label,
                price=row.price,
                is_discontinued=row.is_discontinued,
                match_parameter=getattr(row, "match_parameter", match_param),
                match_reason=row.match_reason,
                score=round(row.score, 4),
            )
        )

    def name_sim(r: SearchResult) -> float:
        """Name-similarity of the medicine name to the user query (higher = better)."""
        return difflib.SequenceMatcher(None, q_norm, r.name.lower()).ratio()

    def sort_bucket(bucket: list[SearchResult]) -> list[SearchResult]:
        """
        Sort within a bucket:
          1. Score DESC  (exact > prefix > token > typo within name tier)
          2. Name-similarity DESC  (name closest to query string first)
          3. Name length ASC  (shorter / simpler name first)
        """
        bucket.sort(key=lambda r: (-r.score, -name_sim(r), len(r.name)))
        return bucket

    # ──────────────────────────────────────────────────────────
    # PARAMETER 1: MEDICINE NAME
    # ──────────────────────────────────────────────────────────

    # 1A. Exact Name Match (<0.5ms via B-Tree)
    exact_res = await session.execute(PG_EXACT_NAME_QUERY, {"q": q_norm, "limit": limit})
    for row in exact_res.fetchall():
        make_and_add(row, name_bucket, "name")

    if name_bucket and name_bucket[0].match_reason == "exact_name" and len(name_bucket) >= limit:
        return name_bucket[:limit]

    # 1B. Prefix Name Match
    remaining = limit - len(name_bucket)
    if remaining > 0:
        existing_list = list(seen_ids) if seen_ids else None
        prefix_res = await session.execute(
            PG_PREFIX_NAME_QUERY,
            {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
        )
        for row in prefix_res.fetchall():
            make_and_add(row, name_bucket, "name")

    # 1C. Exact & Prefix Name Alias Match
    remaining = limit - len(name_bucket)
    if remaining > 0:
        existing_list = list(seen_ids) if seen_ids else None
        alias_res = await session.execute(
            PG_ALIAS_QUERY,
            {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
        )
        for row in alias_res.fetchall():
            make_and_add(row, name_bucket, "name")

    # 1D. Word / Token Match on Name
    remaining = limit - len(name_bucket)
    if remaining > 0 and len(q_norm) >= 3:
        existing_list = list(seen_ids) if seen_ids else None
        token_res = await session.execute(
            PG_TOKEN_NAME_QUERY,
            {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
        )
        for row in token_res.fetchall():
            make_and_add(row, name_bucket, "name")

    # 1E. Fuzzy / Typo Match on Name
    remaining = limit - len(name_bucket)
    if remaining > 0 and len(q_norm) >= 3:
        existing_list = list(seen_ids) if seen_ids else None
        words = re.findall(r"[a-z]+", q_norm)
        numbers = re.findall(r"\d+", q_norm)

        if words and numbers:
            brand_q = " ".join(words)
            num_q = numbers[0]
            fuzzy_decomp_res = await session.execute(
                PG_FUZZY_DECOMPOSED_QUERY,
                {
                    "q": q_norm,
                    "brand_q": brand_q,
                    "num_q": num_q,
                    "remaining": remaining,
                    "existing_ids": existing_list,
                },
            )
            for row in fuzzy_decomp_res.fetchall():
                make_and_add(row, name_bucket, "name")

        remaining = limit - len(name_bucket)
        if remaining > 0:
            existing_list = list(seen_ids) if seen_ids else None
            fuzzy_name_res = await session.execute(
                PG_FUZZY_NAME_QUERY,
                {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
            )
            for row in fuzzy_name_res.fetchall():
                make_and_add(row, name_bucket, "name")

    # Sort name bucket: score DESC → name-similarity DESC → name-length ASC
    sort_bucket(name_bucket)

    # If Name bucket fills the limit → return immediately (no comp/company pollution)
    if len(name_bucket) >= limit:
        return name_bucket[:limit]

    # ──────────────────────────────────────────────────────────
    # PARAMETER 2: COMPOSITION
    # (Only executed if Name matches didn't completely fill limit)
    # ──────────────────────────────────────────────────────────
    remaining = limit - len(name_bucket)
    if remaining > 0 and len(q_norm) >= 3:
        existing_list = list(seen_ids) if seen_ids else None

        # 2A. Prefix on composition
        comp_prefix_res = await session.execute(
            PG_PREFIX_COMPOSITION_QUERY,
            {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
        )
        for row in comp_prefix_res.fetchall():
            make_and_add(row, comp_bucket, "composition")

        # 2B. Fuzzy / Substring on composition
        remaining = limit - len(name_bucket) - len(comp_bucket)
        if remaining > 0 and len(q_norm) >= 4:
            existing_list = list(seen_ids) if seen_ids else None
            comp_fuzzy_res = await session.execute(
                PG_FUZZY_COMPOSITION_QUERY,
                {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
            )
            for row in comp_fuzzy_res.fetchall():
                make_and_add(row, comp_bucket, "composition")

    # Sort composition bucket by name-similarity to query
    sort_bucket(comp_bucket)

    if len(name_bucket) + len(comp_bucket) >= limit:
        return (name_bucket + comp_bucket)[:limit]

    # ──────────────────────────────────────────────────────────
    # PARAMETER 3: COMPANY / MANUFACTURER
    # (Only executed if Name + Composition matches < limit)
    # ──────────────────────────────────────────────────────────
    remaining = limit - len(name_bucket) - len(comp_bucket)
    if remaining > 0 and len(q_norm) >= 3:
        existing_list = list(seen_ids) if seen_ids else None

        # 3A. Prefix on manufacturer
        mfr_prefix_res = await session.execute(
            PG_PREFIX_MANUFACTURER_QUERY,
            {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
        )
        for row in mfr_prefix_res.fetchall():
            make_and_add(row, mfr_bucket, "manufacturer")

        # 3B. Fuzzy on manufacturer
        remaining = limit - len(name_bucket) - len(comp_bucket) - len(mfr_bucket)
        if remaining > 0 and len(q_norm) >= 4:
            existing_list = list(seen_ids) if seen_ids else None
            mfr_fuzzy_res = await session.execute(
                PG_FUZZY_MANUFACTURER_QUERY,
                {"q": q_norm, "remaining": remaining, "existing_ids": existing_list},
            )
            for row in mfr_fuzzy_res.fetchall():
                make_and_add(row, mfr_bucket, "manufacturer")

    # Sort manufacturer bucket by name-similarity to query
    sort_bucket(mfr_bucket)

    # ── Final: strict 3-parameter order — Name → Composition → Manufacturer ──
    return (name_bucket + comp_bucket + mfr_bucket)[:limit]


# ============================================================
# SQLITE IMPLEMENTATION
# ============================================================

async def search_medicines_sqlite(
    session: AsyncSession,
    q_norm: str,
    limit: int = 10,
    *,
    similarity_threshold: float = 0.25,
) -> list[SearchResult]:
    """
    Search medicines on SQLite fallback database strictly adhering to the 3-parameter priority.

    Results are collected into 3 separate buckets:
      name_bucket → composition_bucket → manufacturer_bucket
    Each bucket is sorted by name-similarity to the query before concatenation.
    """
    seen_ids: set[str] = set()
    name_bucket: list[SearchResult] = []
    comp_bucket: list[SearchResult] = []
    mfr_bucket: list[SearchResult] = []

    def make_and_add(row, bucket: list, match_param: str, *, is_disc: bool | None = None):
        if row.medicine_id in seen_ids:
            return
        seen_ids.add(row.medicine_id)
        bucket.append(
            SearchResult(
                medicine_id=row.medicine_id,
                source_id=row.source_id,
                name=row.name,
                manufacturer_name=row.manufacturer_name,
                composition=row.composition,
                pack_size_label=row.pack_size_label,
                price=row.price,
                is_discontinued=bool(row.is_discontinued) if is_disc is None else is_disc,
                match_parameter=getattr(row, "match_parameter", match_param),
                match_reason=row.match_reason,
                score=round(row.score, 4),
            )
        )

    def name_sim(r: SearchResult) -> float:
        return difflib.SequenceMatcher(None, q_norm, r.name.lower()).ratio()

    def sort_bucket(bucket: list[SearchResult]) -> list[SearchResult]:
        bucket.sort(key=lambda r: (-r.score, -name_sim(r), len(r.name)))
        return bucket

    # ──────────────────────────────────────────────────
    # PARAMETER 1: MEDICINE NAME
    # ──────────────────────────────────────────────────

    # 1A. Exact Name
    exact_res = await session.execute(SQLITE_EXACT_NAME_QUERY, {"q": q_norm, "limit": limit})
    for row in exact_res.fetchall():
        make_and_add(row, name_bucket, "name")

    if name_bucket and name_bucket[0].match_reason == "exact_name" and len(name_bucket) >= limit:
        return name_bucket[:limit]

    # 1B. Prefix Name
    remaining = limit - len(name_bucket)
    if remaining > 0:
        prefix_res = await session.execute(SQLITE_PREFIX_NAME_QUERY, {"q": q_norm, "remaining": remaining})
        for row in prefix_res.fetchall():
            make_and_add(row, name_bucket, "name")

    # 1C. Exact/Prefix Alias
    remaining = limit - len(name_bucket)
    if remaining > 0:
        alias_res = await session.execute(SQLITE_ALIAS_QUERY, {"q": q_norm, "remaining": remaining})
        for row in alias_res.fetchall():
            make_and_add(row, name_bucket, "name")

    # 1D. Token Name
    remaining = limit - len(name_bucket)
    if remaining > 0 and len(q_norm) >= 3:
        token_res = await session.execute(SQLITE_TOKEN_NAME_QUERY, {"q": q_norm, "remaining": remaining})
        for row in token_res.fetchall():
            make_and_add(row, name_bucket, "name")

    # 1E. Fuzzy/Typo on Name via difflib SequenceMatcher
    remaining = limit - len(name_bucket)
    if remaining > 0:
        words = re.findall(r"[a-z]+", q_norm)
        numbers = re.findall(r"\d+", q_norm)
        first_char = q_norm[0] if q_norm else ""
        first_word = words[0] if words else ""
        first_num = numbers[0] if numbers else ""

        cand_res = await session.execute(
            SQLITE_CANDIDATES_QUERY,
            {
                "first_char": first_char,
                "word": first_word[:4] if len(first_word) >= 4 else first_word,
                "num": first_num,
            },
        )
        candidates = cand_res.fetchall()
        scored_candidates = []
        for cand in candidates:
            if cand.medicine_id in seen_ids:
                continue
            sim = difflib.SequenceMatcher(None, q_norm, cand.name_normalized).ratio()
            if sim >= 0.35:
                scored_candidates.append((cand, sim))

        # Sort by similarity DESC, then name length ASC for tie-breaking
        scored_candidates.sort(key=lambda x: (-x[1], len(x[0].name)))
        for cand, sim in scored_candidates[:remaining]:
            seen_ids.add(cand.medicine_id)
            name_bucket.append(
                SearchResult(
                    medicine_id=cand.medicine_id,
                    source_id=cand.source_id,
                    name=cand.name,
                    manufacturer_name=cand.manufacturer_name,
                    composition=cand.composition,
                    pack_size_label=cand.pack_size_label,
                    price=cand.price,
                    is_discontinued=bool(cand.is_discontinued),
                    match_parameter="name",
                    match_reason="typo_match",
                    score=round(0.75 + 0.09 * sim, 4),
                )
            )

    # Sort name bucket: score DESC → name-similarity DESC → name-length ASC
    sort_bucket(name_bucket)

    # If Name bucket fills the limit → return immediately!
    if len(name_bucket) >= limit:
        return name_bucket[:limit]

    # ──────────────────────────────────────────────────
    # PARAMETER 2: COMPOSITION
    # ──────────────────────────────────────────────────
    remaining = limit - len(name_bucket)
    if remaining > 0 and len(q_norm) >= 3:
        comp_res = await session.execute(
            SQLITE_PREFIX_COMPOSITION_QUERY,
            {"q": q_norm, "remaining": remaining},
        )
        for row in comp_res.fetchall():
            make_and_add(row, comp_bucket, "composition")

    sort_bucket(comp_bucket)

    if len(name_bucket) + len(comp_bucket) >= limit:
        return (name_bucket + comp_bucket)[:limit]

    # ──────────────────────────────────────────────────
    # PARAMETER 3: COMPANY / MANUFACTURER
    # ──────────────────────────────────────────────────
    remaining = limit - len(name_bucket) - len(comp_bucket)
    if remaining > 0 and len(q_norm) >= 3:
        mfr_res = await session.execute(
            SQLITE_PREFIX_MANUFACTURER_QUERY,
            {"q": q_norm, "remaining": remaining},
        )
        for row in mfr_res.fetchall():
            make_and_add(row, mfr_bucket, "manufacturer")

    sort_bucket(mfr_bucket)

    # Final: strict 3-parameter order — Name → Composition → Manufacturer
    return (name_bucket + comp_bucket + mfr_bucket)[:limit]



# ============================================================
# MAIN SEARCH ENTRY POINT WITH LRU CACHING
# ============================================================

async def search_medicines(
    session: AsyncSession,
    query: str,
    limit: int = 10,
    *,
    similarity_threshold: float = 0.25,
) -> list[SearchResult]:
    """
    Search medicines strictly across 3 prioritized parameters:
    1. Medicine Name
    2. Composition
    3. Company / Manufacturer

    Employs an in-memory cache for instant (<5ms) responses on repeated queries.
    """
    q_norm = normalize_medicine_name(query)
    if not q_norm:
        return []

    bind = session.bind
    dialect = bind.dialect.name if bind else "postgresql"
    cache_key = f"{dialect}:{limit}:{q_norm}"

    # Check LRU cache
    now = time.time()
    if cache_key in _CACHE:
        cached_time, cached_res = _CACHE[cache_key]
        if now - cached_time < _CACHE_TTL_SECONDS:
            return cached_res

    if dialect == "sqlite":
        results = await search_medicines_sqlite(
            session, q_norm, limit=limit, similarity_threshold=similarity_threshold
        )
    else:
        results = await search_medicines_postgres(
            session, q_norm, limit=limit, similarity_threshold=similarity_threshold
        )

    # Store in cache
    if len(_CACHE) >= _MAX_CACHE_SIZE:
        # Evict oldest entry
        oldest_key = min(_CACHE.keys(), key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest_key, None)
    _CACHE[cache_key] = (now, results)

    return results
